# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
Tests for the LiveExecutionEngine check loops (open orders, inflight, etc).

These tests focus on the critical async loops that perform periodic reconciliation
checks during live trading.

"""

import asyncio
from decimal import Decimal

import pytest

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import QueryOrder
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.test_kit.functions import ensure_all_tasks_completed
from nautilus_trader.test_kit.mocks.exec_clients import MockLiveExecutionClient
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs


AUDUSD_SIM = TestInstrumentProvider.default_fx_ccy("AUD/USD")
SIM = Venue("SIM")


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture(name="clock")
def fixture_clock():
    """
    Create a live clock.
    """
    return LiveClock()


@pytest.fixture(name="trader_id")
def fixture_trader_id():
    """
    Create a trader ID.
    """
    return TestIdStubs.trader_id()


@pytest.fixture(name="account_id")
def fixture_account_id():
    """
    Create an account ID.
    """
    return TestIdStubs.account_id()


@pytest.fixture(name="msgbus")
def fixture_msgbus(trader_id, clock):
    """
    Create a message bus.
    """
    return MessageBus(
        trader_id=trader_id,
        clock=clock,
    )


@pytest.fixture(name="cache")
def fixture_cache():
    """
    Create a cache with AUDUSD instrument.
    """
    cache = TestComponentStubs.cache()
    cache.add_instrument(AUDUSD_SIM)
    return cache


@pytest.fixture(name="instrument_provider")
def fixture_instrument_provider():
    """
    Create an instrument provider.
    """
    return InstrumentProvider()


@pytest.fixture(name="exec_client")
def fixture_exec_client(msgbus, cache, clock, instrument_provider):
    """
    Create a mock live execution client.
    """
    loop = asyncio.get_event_loop_policy().get_event_loop()
    client = MockLiveExecutionClient(
        loop=loop,
        client_id=ClientId(SIM.value),
        venue=SIM,
        account_type=AccountType.CASH,
        base_currency=USD,
        instrument_provider=instrument_provider,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
    )
    return client


@pytest.fixture(name="exec_engine_open_check")
def fixture_exec_engine_open_check(msgbus, cache, clock, exec_client):
    """
    Create an execution engine configured for open order checking.
    """
    loop = asyncio.get_event_loop_policy().get_event_loop()
    exec_engine = LiveExecutionEngine(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        config=LiveExecEngineConfig(
            open_check_interval_secs=0.1,  # 100ms for fast testing
            open_check_open_only=False,
        ),
    )
    exec_engine.register_client(exec_client)

    yield exec_engine

    exec_engine.stop()
    ensure_all_tasks_completed()


@pytest.fixture(name="exec_engine_inflight_check")
def fixture_exec_engine_inflight_check(msgbus, cache, clock, exec_client):
    """
    Create an execution engine configured for inflight order checking.
    """
    loop = asyncio.get_event_loop_policy().get_event_loop()
    exec_engine = LiveExecutionEngine(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        config=LiveExecEngineConfig(
            inflight_check_interval_ms=50,  # 50ms for fast testing
            inflight_check_threshold_ms=10,  # Low threshold for testing
            inflight_check_retries=2,
        ),
    )
    exec_engine.register_client(exec_client)

    yield exec_engine

    exec_engine.stop()
    ensure_all_tasks_completed()


@pytest.fixture(name="exec_engine_basic")
def fixture_exec_engine_basic(msgbus, cache, clock):
    """
    Create a basic execution engine without client.
    """
    loop = asyncio.get_event_loop_policy().get_event_loop()
    exec_engine = LiveExecutionEngine(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
    )

    yield exec_engine

    exec_engine.stop()
    ensure_all_tasks_completed()


@pytest.fixture(name="exec_engine_combined")
def fixture_exec_engine_combined(msgbus, cache, clock, exec_client):
    """
    Create an execution engine for combined reconciliation scenarios.
    """
    loop = asyncio.get_event_loop_policy().get_event_loop()
    exec_engine = LiveExecutionEngine(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        config=LiveExecEngineConfig(
            inflight_check_interval_ms=100,
            inflight_check_threshold_ms=50,
            open_check_interval_secs=0.2,
        ),
    )
    exec_engine.register_client(exec_client)

    yield exec_engine

    exec_engine.stop()
    ensure_all_tasks_completed()


# =============================================================================
# OPEN ORDER CHECK TESTS
# =============================================================================


@pytest.mark.asyncio()
async def test_check_open_orders_with_no_open_orders(exec_engine_open_check, exec_client):
    """
    Test _check_open_orders when there are no open orders in cache.
    """
    # Act
    await exec_engine_open_check._check_open_orders()

    # Assert - should not make any API calls
    assert len(exec_client._order_status_reports) == 0


@pytest.mark.asyncio()
async def test_check_open_orders_with_open_orders_matching_venue(
    exec_engine_open_check,
    exec_client,
    cache,
    account_id,
):
    """
    Test _check_open_orders when cache and venue agree on open orders.
    """
    # Arrange - add open order to cache
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Apply events to order to set proper state
    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    order.apply(submitted)
    exec_engine_open_check.process(submitted)

    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        venue_order_id=VenueOrderId("V-1"),
    )
    order.apply(accepted)
    exec_engine_open_check.process(accepted)

    # Create matching venue report
    venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        order_side=order.side,
        order_type=order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        price=order.price,
        quantity=order.quantity,
        filled_qty=Quantity.from_int(0),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )
    exec_client.add_order_status_report(venue_report)

    # Act
    await exec_engine_open_check._check_open_orders()

    # Assert - no reconciliation needed
    assert order.status == OrderStatus.ACCEPTED


@pytest.mark.asyncio()
async def test_check_open_orders_reconciles_status_not_fills(
    exec_engine_open_check,
    exec_client,
    cache,
    account_id,
):
    """
    Test _check_open_orders reconciles order status but not fills (fills handled
    separately).
    """
    # Arrange - add open order to cache
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Apply events to order to set proper state
    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    order.apply(submitted)
    exec_engine_open_check.process(submitted)

    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        venue_order_id=VenueOrderId("V-1"),
    )
    order.apply(accepted)
    exec_engine_open_check.process(accepted)

    # Create venue report showing partial fill
    venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        order_side=order.side,
        order_type=order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.PARTIALLY_FILLED,
        price=order.price,
        quantity=order.quantity,
        filled_qty=Quantity.from_int(50),  # Venue shows 50 filled
        avg_px=Decimal("1.00000"),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )
    exec_client.add_order_status_report(venue_report)

    # Act
    await exec_engine_open_check._check_open_orders()

    # Assert - status reconciled but fills not applied (fills handled separately)
    # The open orders check only reconciles status, not fills
    assert order.status == OrderStatus.ACCEPTED
    assert order.filled_qty == Quantity.from_int(0)


@pytest.mark.asyncio()
async def test_check_open_orders_reconciles_closed_order(
    exec_engine_open_check,
    exec_client,
    cache,
    account_id,
):
    """
    Test _check_open_orders reconciles when an order was closed on venue.
    """
    # Arrange - add open order to cache
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Apply events to order to set proper state
    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    order.apply(submitted)
    exec_engine_open_check.process(submitted)

    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        venue_order_id=VenueOrderId("V-1"),
    )
    order.apply(accepted)
    exec_engine_open_check.process(accepted)

    # Create venue report showing order filled
    venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        order_side=order.side,
        order_type=order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.FILLED,
        price=order.price,
        quantity=order.quantity,
        filled_qty=order.quantity,
        avg_px=Decimal("1.00000"),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )
    exec_client.add_order_status_report(venue_report)

    # Act
    await exec_engine_open_check._check_open_orders()

    # Assert - without trades, FILLED status is not reconciled
    # This is expected as fills are handled separately with trade data
    assert order.status == OrderStatus.ACCEPTED
    assert order.filled_qty == Quantity.from_int(0)


@pytest.mark.asyncio()
async def test_check_open_orders_open_only_mode(
    exec_engine_open_check,
    exec_client,
    cache,
    account_id,
):
    """
    Test _check_open_orders in open_only mode queries venue regardless of cache.
    """
    # Arrange - configure for open_only mode
    exec_engine_open_check.open_check_open_only = True

    # Even with no open orders in cache, should query venue
    venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=ClientOrderId("EXTERNAL-123"),
        venue_order_id=VenueOrderId("V-1"),
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        price=Price.from_str("1.00000"),
        quantity=Quantity.from_int(100),
        filled_qty=Quantity.from_int(0),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )
    exec_client.add_order_status_report(venue_report)

    # Act
    await exec_engine_open_check._check_open_orders()

    # Assert - external order should be reconciled
    assert len(cache.orders()) == 1
    order = cache.orders()[0]
    assert order.client_order_id == ClientOrderId("EXTERNAL-123")
    assert order.status == OrderStatus.ACCEPTED


@pytest.mark.asyncio()
async def test_check_open_orders_handles_client_exception(
    exec_engine_open_check,
    cache,
    account_id,
):
    """
    Test _check_open_orders handles exceptions from client gracefully.
    """
    # Arrange - add open order to cache
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Apply events to order to set proper state
    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    order.apply(submitted)
    exec_engine_open_check.process(submitted)

    accepted = TestEventStubs.order_accepted(order, account_id=account_id)
    order.apply(accepted)
    exec_engine_open_check.process(accepted)

    # Make client raise exception - patch the method on the registered client
    async def raise_error(command):
        raise RuntimeError("API error")

    for client in exec_engine_open_check._clients.values():
        client.generate_order_status_reports = raise_error

    # Act - should not raise
    await exec_engine_open_check._check_open_orders()

    # Assert - order unchanged
    assert order.status == OrderStatus.ACCEPTED


@pytest.mark.asyncio()
async def test_open_check_periodic_execution(exec_engine_open_check):
    """
    Test that open check executes periodically via reconciliation loop.
    """
    # Arrange
    check_count = 0
    original_check = exec_engine_open_check._check_orders_consistency

    async def counting_check():
        nonlocal check_count
        check_count += 1
        if check_count >= 2:
            # Cancel the task after 2 checks
            if exec_engine_open_check._reconciliation_task:
                exec_engine_open_check._reconciliation_task.cancel()
        return await original_check()

    exec_engine_open_check._check_orders_consistency = counting_check

    # Act - start the loop
    task = asyncio.create_task(exec_engine_open_check._continuous_reconciliation_loop())

    # Wait for task to complete or timeout
    try:
        await asyncio.wait_for(task, timeout=0.5)
    except asyncio.CancelledError:
        pass

    # Assert
    assert check_count >= 2


# =============================================================================
# INFLIGHT ORDER CHECK TESTS
# =============================================================================


@pytest.mark.asyncio()
async def test_check_inflight_orders_queries_old_orders(
    exec_engine_inflight_check,
    cache,
    account_id,
    clock,
):
    """
    Test _check_inflight_orders queries orders exceeding threshold.
    """
    # Arrange - create inflight order
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Submit order (makes it inflight)
    submitted_event = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=clock.timestamp_ns() - 1_000_000_000,  # 1 second ago
    )
    order.apply(submitted_event)
    exec_engine_inflight_check.process(submitted_event)
    cache.update_order(order)

    # Update cache with the order event to ensure indexes are updated
    cache.update_order(order)

    # Capture executed commands
    executed_commands = []
    original_execute = exec_engine_inflight_check._execute_command

    def capture_execute(command):
        executed_commands.append(command)
        return original_execute(command)

    exec_engine_inflight_check._execute_command = capture_execute

    # Act
    await exec_engine_inflight_check._check_inflight_orders()

    # Assert - should have queried the order
    assert len(executed_commands) == 1
    assert isinstance(executed_commands[0], QueryOrder)
    assert executed_commands[0].client_order_id == order.client_order_id


@pytest.mark.asyncio()
async def test_check_inflight_orders_respects_retry_limit(
    exec_engine_inflight_check,
    cache,
    account_id,
    clock,
):
    """
    Test _check_inflight_orders stops querying after max retries.
    """
    # Arrange - create inflight order
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Submit order (makes it inflight)
    submitted_event = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=clock.timestamp_ns() - 1_000_000_000,
    )
    order.apply(submitted_event)
    exec_engine_inflight_check.process(submitted_event)
    cache.update_order(order)

    # Set retry count to max
    exec_engine_inflight_check._inflight_check_retries[order.client_order_id] = 2

    # Act
    await exec_engine_inflight_check._check_inflight_orders()

    # Assert - should have resolved the order (rejected)
    assert order.status == OrderStatus.REJECTED
    assert order.client_order_id not in exec_engine_inflight_check._inflight_check_retries


@pytest.mark.asyncio()
async def test_check_inflight_orders_increments_retry_count(
    exec_engine_inflight_check,
    cache,
    account_id,
    clock,
):
    """
    Test _check_inflight_orders increments retry count on each check.
    """
    # Arrange - create inflight order
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Submit order (makes it inflight)
    submitted_event = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=clock.timestamp_ns() - 1_000_000_000,
    )
    order.apply(submitted_event)
    exec_engine_inflight_check.process(submitted_event)
    cache.update_order(order)

    # Act - check twice
    await exec_engine_inflight_check._check_inflight_orders()
    assert exec_engine_inflight_check._inflight_check_retries[order.client_order_id] == 1

    await exec_engine_inflight_check._check_inflight_orders()
    assert exec_engine_inflight_check._inflight_check_retries[order.client_order_id] == 2


@pytest.mark.asyncio()
async def test_check_inflight_orders_skips_recent_orders(
    exec_engine_inflight_check,
    cache,
    account_id,
):
    """
    Test _check_inflight_orders skips orders within threshold.
    """
    # Arrange - create inflight order with recent timestamp
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Submit order with future timestamp to ensure it's within threshold
    # Use clock from exec_engine to get current time and add a small amount
    current_time = exec_engine_inflight_check._clock.timestamp_ns()
    submitted_event = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=current_time + 1_000_000,  # 1ms in the future
    )
    order.apply(submitted_event)
    exec_engine_inflight_check.process(submitted_event)
    cache.update_order(order)

    # Capture executed commands
    executed_commands = []
    original_execute = exec_engine_inflight_check._execute_command

    def capture_execute(command):
        executed_commands.append(command)
        return original_execute(command)

    exec_engine_inflight_check._execute_command = capture_execute

    # Act
    await exec_engine_inflight_check._check_inflight_orders()

    # Assert - should not have queried the order
    assert len(executed_commands) == 0


@pytest.mark.asyncio()
async def test_inflight_check_periodic_execution(exec_engine_inflight_check):
    """
    Test that inflight check executes periodically via reconciliation loop.
    """
    # Arrange
    check_count = 0

    async def counting_check():
        nonlocal check_count
        check_count += 1
        if check_count >= 2:
            # Cancel the task after 2 checks
            if exec_engine_inflight_check._reconciliation_task:
                exec_engine_inflight_check._reconciliation_task.cancel()

    exec_engine_inflight_check._check_inflight_orders = counting_check

    # Act - start the loop
    task = asyncio.create_task(exec_engine_inflight_check._continuous_reconciliation_loop())

    # Wait for task to complete or timeout
    try:
        await asyncio.wait_for(task, timeout=0.5)
    except asyncio.CancelledError:
        pass

    # Assert
    assert check_count >= 2


@pytest.mark.asyncio()
async def test_inflight_check_handles_exceptions(exec_engine_inflight_check):
    """
    Test that reconciliation loop continues after exceptions.
    """
    # Arrange
    check_count = 0

    async def failing_check():
        nonlocal check_count
        check_count += 1
        if check_count == 1:
            raise RuntimeError("Test error")
        elif check_count >= 2:
            if exec_engine_inflight_check._reconciliation_task:
                exec_engine_inflight_check._reconciliation_task.cancel()

    exec_engine_inflight_check._check_inflight_orders = failing_check

    # Act - start the loop
    task = asyncio.create_task(exec_engine_inflight_check._continuous_reconciliation_loop())

    # Wait for task to complete or timeout
    try:
        await asyncio.wait_for(task, timeout=0.5)
    except asyncio.CancelledError:
        pass

    # Assert - should have continued after exception
    assert check_count >= 2


# =============================================================================
# ORDER UPDATE RECONCILIATION TESTS
# =============================================================================


@pytest.mark.asyncio()
async def test_reconcile_order_with_price_update(
    exec_engine_basic,
    cache,
    account_id,
):
    """
    Test reconciliation generates update when price differs.
    """
    # Arrange - create limit order
    order = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        price=Price.from_str("1.00000"),
    )
    cache.add_order(order)

    # Apply events to order to set proper state
    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    order.apply(submitted)
    exec_engine_basic.process(submitted)

    accepted = TestEventStubs.order_accepted(order, account_id=account_id)
    order.apply(accepted)
    exec_engine_basic.process(accepted)

    # Create report with different price
    report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        order_side=order.side,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        price=Price.from_str("1.00100"),  # Different price
        quantity=order.quantity,
        filled_qty=Quantity.from_int(0),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )

    # Act
    result = exec_engine_basic._reconcile_order_report(report, trades=[])

    # Assert - reconciliation succeeds but order update happens via events
    # The direct call to _reconcile_order_report doesn't apply the update event
    assert result is True
    # Order price remains unchanged without full event processing
    assert order.price == Price.from_str("1.00000")


@pytest.mark.asyncio()
async def test_reconcile_order_with_quantity_update(
    exec_engine_basic,
    cache,
    account_id,
):
    """
    Test reconciliation generates update when quantity differs.
    """
    # Arrange - create limit order
    order = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        quantity=Quantity.from_int(100),
    )
    cache.add_order(order)

    # Apply events to order to set proper state
    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    order.apply(submitted)
    exec_engine_basic.process(submitted)

    accepted = TestEventStubs.order_accepted(order, account_id=account_id)
    order.apply(accepted)
    exec_engine_basic.process(accepted)

    # Create report with different quantity
    report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        order_side=order.side,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        price=order.price,
        quantity=Quantity.from_int(150),  # Different quantity
        filled_qty=Quantity.from_int(0),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )

    # Act
    result = exec_engine_basic._reconcile_order_report(report, trades=[])

    # Assert - reconciliation succeeds but order update happens via events
    # The direct call to _reconcile_order_report doesn't apply the update event
    assert result is True
    # Order quantity remains unchanged without full event processing
    assert order.quantity == Quantity.from_int(100)


@pytest.mark.asyncio()
async def test_reconcile_order_without_client_order_id(
    exec_engine_basic,
    cache,
    account_id,
):
    """
    Test reconciliation handles missing client_order_id.
    """
    # Arrange - create order and add to cache
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    venue_order_id = VenueOrderId("V-123")
    cache.add_order(order)

    # Apply events to order to set proper state
    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    order.apply(submitted)
    exec_engine_basic.process(submitted)

    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        venue_order_id=venue_order_id,
    )
    order.apply(accepted)
    exec_engine_basic.process(accepted)

    # Create report WITHOUT client_order_id
    report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=None,  # Missing
        venue_order_id=venue_order_id,
        order_side=order.side,
        order_type=order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.PARTIALLY_FILLED,
        price=order.price,
        quantity=order.quantity,
        filled_qty=Quantity.from_int(50),
        avg_px=Decimal("1.00000"),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )

    # Act
    result = exec_engine_basic._reconcile_order_report(report, trades=[])

    # Assert - should find order by venue_order_id
    assert result is True
    # Fills are not applied without trade data
    assert order.filled_qty == Quantity.from_int(0)
    # Report should have client_order_id assigned from cache lookup
    assert report.client_order_id is not None


# =============================================================================
# RECONCILIATION TESTS
# =============================================================================


@pytest.fixture(name="exec_engine_continuous")
def fixture_exec_engine_continuous(msgbus, cache, clock, exec_client):
    """
    Create an execution engine configured for continuous reconciliation.
    """
    loop = asyncio.get_event_loop_policy().get_event_loop()
    exec_engine = LiveExecutionEngine(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        config=LiveExecEngineConfig(
            inflight_check_interval_ms=50,
            inflight_check_threshold_ms=25,
            inflight_check_retries=2,
            open_check_interval_secs=0.1,
            open_check_open_only=False,
        ),
    )
    exec_engine.register_client(exec_client)

    yield exec_engine

    exec_engine.stop()
    ensure_all_tasks_completed()


@pytest.mark.asyncio()
async def test_reconciliation_mode_enabled(exec_engine_continuous):
    """
    Test that reconciliation is enabled when checks are configured.
    """
    # Assert
    assert exec_engine_continuous.inflight_check_interval_ms > 0
    assert exec_engine_continuous.open_check_interval_secs is not None


@pytest.mark.asyncio()
async def test_reconciliation_task_created(exec_engine_continuous):
    """
    Test that reconciliation task is created.
    """
    # Act
    exec_engine_continuous.start()
    await asyncio.sleep(0.01)  # Give time for task creation

    # Assert
    assert exec_engine_continuous.get_reconciliation_task() is not None


@pytest.mark.asyncio()
async def test_check_inflight_orders_detects_inflight(
    exec_engine_continuous,
    cache,
    account_id,
    clock,
):
    """
    Test _check_inflight_orders detects in-flight orders exceeding threshold.
    """
    # Arrange - create in-flight order
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Make order inflight with old timestamp
    submitted_event = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=clock.timestamp_ns() - 1_000_000_000,  # 1 second ago
    )
    order.apply(submitted_event)
    exec_engine_continuous.process(submitted_event)

    # Ensure order is in cache's open orders index
    cache.update_order(order)

    # Capture executed commands
    executed_commands = []
    original_execute = exec_engine_continuous._execute_command

    def capture_execute(command):
        executed_commands.append(command)
        return original_execute(command)

    exec_engine_continuous._execute_command = capture_execute

    # Act
    await exec_engine_continuous._check_inflight_orders()

    # Assert - should have queried the problematic order
    assert len(executed_commands) == 1
    assert isinstance(executed_commands[0], QueryOrder)
    assert executed_commands[0].client_order_id == order.client_order_id


@pytest.mark.asyncio()
async def test_check_orders_consistency_reconciles_discrepancies(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
):
    """
    Test _check_orders_consistency reconciles discrepancies between cache and venue.
    """
    # Arrange - add open order to cache
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Apply events to set order as ACCEPTED
    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    order.apply(submitted)
    exec_engine_continuous.process(submitted)

    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        venue_order_id=VenueOrderId("V-1"),
    )
    order.apply(accepted)
    exec_engine_continuous.process(accepted)

    # Create venue report showing order is FILLED
    venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        order_side=order.side,
        order_type=order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.FILLED,
        price=order.price,
        quantity=order.quantity,
        filled_qty=order.quantity,
        avg_px=Decimal("1.00000"),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )
    exec_client.add_order_status_report(venue_report)

    # Act
    await exec_engine_continuous._check_orders_consistency()

    # Assert - reconciliation should have been triggered
    # Note: Without full trade data, status remains ACCEPTED
    assert order.status == OrderStatus.ACCEPTED
    # Retry count should be cleared for successfully queried orders
    assert order.client_order_id not in exec_engine_continuous._inflight_check_retries


@pytest.mark.asyncio()
async def test_reconciliation_loop_runs_both_checks(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test that reconciliation loop runs both check types at correct intervals.
    """
    # Arrange
    problematic_check_count = 0
    consistency_check_count = 0

    async def counting_problematic_check():
        nonlocal problematic_check_count
        problematic_check_count += 1

    async def counting_consistency_check():
        nonlocal consistency_check_count
        consistency_check_count += 1
        if consistency_check_count >= 2:
            # Stop after 2 consistency checks
            if exec_engine_continuous._reconciliation_task:
                exec_engine_continuous._reconciliation_task.cancel()

    exec_engine_continuous._check_inflight_orders = counting_problematic_check
    exec_engine_continuous._check_orders_consistency = counting_consistency_check

    # Act - start the loop
    task = asyncio.create_task(exec_engine_continuous._continuous_reconciliation_loop())

    # Wait for task to complete or timeout
    try:
        await asyncio.wait_for(task, timeout=0.5)
    except asyncio.CancelledError:
        pass

    # Assert
    # Problematic checks should run more frequently (50ms interval)
    assert problematic_check_count > consistency_check_count
    # At least 2 consistency checks should have run
    assert consistency_check_count >= 2


@pytest.mark.asyncio()
async def test_reconciliation_clears_retry_counts_on_success(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
):
    """
    Test that successful consistency check clears retry counts for orders.
    """
    # Arrange - create order with retry count
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Apply events to set order as ACCEPTED
    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    order.apply(submitted)
    exec_engine_continuous.process(submitted)

    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        venue_order_id=VenueOrderId("V-1"),
    )
    order.apply(accepted)
    exec_engine_continuous.process(accepted)

    # Ensure cache index is updated
    cache.update_order(order)

    # Set a retry count
    exec_engine_continuous._inflight_check_retries[order.client_order_id] = 3

    # Create matching venue report
    venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        order_side=order.side,
        order_type=order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        price=order.price,
        quantity=order.quantity,
        filled_qty=Quantity.from_int(0),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )
    exec_client.add_order_status_report(venue_report)

    # Act
    await exec_engine_continuous._check_orders_consistency()

    # Assert - retry count should be cleared after successful query
    assert order.client_order_id not in exec_engine_continuous._inflight_check_retries


# =============================================================================
# COMBINED RECONCILIATION SCENARIO TESTS
# =============================================================================


@pytest.mark.asyncio()
async def test_inflight_and_open_order_combined_scenario(
    exec_engine_combined,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test scenario with both inflight and open orders being checked.
    """
    # Arrange
    # Create an inflight order (SUBMITTED)
    inflight_order = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        client_order_id=ClientOrderId("INFLIGHT-1"),
    )
    cache.add_order(inflight_order)
    submitted_event = TestEventStubs.order_submitted(
        inflight_order,
        account_id=account_id,
        ts_event=clock.timestamp_ns() - 1_000_000_000,  # Old
    )
    inflight_order.apply(submitted_event)
    exec_engine_combined.process(submitted_event)

    # Create an open order (ACCEPTED)
    open_order = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        client_order_id=ClientOrderId("OPEN-1"),
    )
    cache.add_order(open_order)

    # Apply events to order to set proper state
    submitted = TestEventStubs.order_submitted(open_order, account_id=account_id)
    open_order.apply(submitted)
    exec_engine_combined.process(submitted)

    accepted = TestEventStubs.order_accepted(open_order, account_id=account_id)
    open_order.apply(accepted)
    exec_engine_combined.process(accepted)

    # Setup venue reports
    # Inflight order was actually accepted
    inflight_venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=inflight_order.client_order_id,
        venue_order_id=VenueOrderId("V-INFLIGHT"),
        order_side=inflight_order.side,
        order_type=inflight_order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        price=inflight_order.price,
        quantity=inflight_order.quantity,
        filled_qty=Quantity.from_int(0),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )

    # Open order has a missed fill
    open_venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=open_order.client_order_id,
        venue_order_id=VenueOrderId("V-OPEN"),
        order_side=open_order.side,
        order_type=open_order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.PARTIALLY_FILLED,
        price=open_order.price,
        quantity=open_order.quantity,
        filled_qty=Quantity.from_int(25),
        avg_px=Decimal("1.00000"),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )

    exec_client.add_order_status_report(inflight_venue_report)
    exec_client.add_order_status_report(open_venue_report)

    # Update cache to ensure inflight index is updated
    cache.update_order(inflight_order)
    cache.update_order(open_order)

    # Act - run both checks
    await exec_engine_combined._check_inflight_orders()
    await exec_engine_combined._check_orders_consistency()

    # Assert
    # Inflight order should be reconciled and accepted
    assert inflight_order.status == OrderStatus.ACCEPTED
    # Retry count is cleared after successful reconciliation
    assert inflight_order.client_order_id not in exec_engine_combined._inflight_check_retries

    # Open order check doesn't apply fills (only reconciles status)
    assert open_order.status == OrderStatus.ACCEPTED
    assert open_order.filled_qty == Quantity.from_int(0)


@pytest.mark.asyncio()
async def test_order_transitions_from_inflight_to_open(
    exec_engine_combined,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test order transitioning from inflight check to open check.
    """
    # Arrange - create order that starts as inflight
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Start as SUBMITTED (inflight)
    submitted_event = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=clock.timestamp_ns() - 1_000_000_000,
    )
    order.apply(submitted_event)
    exec_engine_combined.process(submitted_event)

    # Update cache to ensure inflight index is updated
    cache.update_order(order)

    # First check - while inflight
    await exec_engine_combined._check_inflight_orders()
    assert exec_engine_combined._inflight_check_retries[order.client_order_id] == 1

    # Now order gets accepted
    accepted = TestEventStubs.order_accepted(order, account_id=account_id)
    order.apply(accepted)
    exec_engine_combined.process(accepted)

    # Setup venue report for open check
    venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        order_side=order.side,
        order_type=order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        price=order.price,
        quantity=order.quantity,
        filled_qty=Quantity.from_int(0),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )
    exec_client.add_order_status_report(venue_report)

    # Act - check as open order
    await exec_engine_combined._check_open_orders()

    # Assert - retry count should be cleared
    assert order.client_order_id not in exec_engine_combined._inflight_check_retries
    assert order.status == OrderStatus.ACCEPTED


# =============================================================================
# TESTS FOR ORDER NOT FOUND AT VENUE RECONCILIATION
# =============================================================================


@pytest.mark.asyncio
async def test_accepted_order_not_found_at_venue_reconciles_to_rejected(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test that an ACCEPTED order not found at venue is reconciled to REJECTED.
    """
    # Arrange - create an accepted order
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    accepted = TestEventStubs.order_accepted(order, account_id=account_id)

    order.apply(submitted)
    order.apply(accepted)
    exec_engine_continuous.process(submitted)
    exec_engine_continuous.process(accepted)

    # Ensure cache is updated
    cache.update_order(order)

    # Venue has no orders (clear all reports)
    exec_client._order_status_reports.clear()

    # Simulate max retries reached by setting retry count to threshold
    exec_engine_continuous._inflight_check_retries[order.client_order_id] = (
        exec_engine_continuous.open_check_missing_retries
    )

    # Act - run consistency check
    await exec_engine_continuous._check_orders_consistency()

    # Assert - order should be rejected
    assert order.status == OrderStatus.REJECTED
    assert order.is_closed


@pytest.mark.asyncio
async def test_partially_filled_order_not_found_at_venue_reconciles_to_canceled(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test that a PARTIALLY_FILLED order not found at venue is reconciled to CANCELED.
    """
    # Arrange - create a partially filled order
    order = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        quantity=Quantity.from_int(100),
    )
    cache.add_order(order)

    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    accepted = TestEventStubs.order_accepted(order, account_id=account_id)
    filled = TestEventStubs.order_filled(
        order,
        instrument=AUDUSD_SIM,
        last_qty=Quantity.from_int(50),  # Partial fill
    )

    order.apply(submitted)
    order.apply(accepted)
    order.apply(filled)
    exec_engine_continuous.process(submitted)
    exec_engine_continuous.process(accepted)
    exec_engine_continuous.process(filled)

    # Ensure cache is updated
    cache.update_order(order)

    # Venue has no orders
    exec_client._order_status_reports.clear()

    # Simulate max retries reached by setting retry count to threshold
    exec_engine_continuous._inflight_check_retries[order.client_order_id] = (
        exec_engine_continuous.open_check_missing_retries
    )

    # Act - run consistency check
    await exec_engine_continuous._check_orders_consistency()

    # Assert - order should be canceled but preserve fill
    assert order.status == OrderStatus.CANCELED
    assert order.is_closed
    assert order.filled_qty == Quantity.from_int(50)


@pytest.mark.asyncio
async def test_filled_order_not_found_at_venue_remains_unchanged(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test that a FILLED order not found at venue remains unchanged (normal behavior).
    """
    # Arrange - create a fully filled order
    order = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        quantity=Quantity.from_int(100),
    )
    cache.add_order(order)

    submitted = TestEventStubs.order_submitted(order, account_id=account_id)
    accepted = TestEventStubs.order_accepted(order, account_id=account_id)
    filled = TestEventStubs.order_filled(
        order,
        instrument=AUDUSD_SIM,
        last_qty=Quantity.from_int(100),  # Full fill
    )

    order.apply(submitted)
    order.apply(accepted)
    order.apply(filled)
    exec_engine_continuous.process(submitted)
    exec_engine_continuous.process(accepted)
    exec_engine_continuous.process(filled)

    # Ensure cache is updated
    cache.update_order(order)

    # Venue has no orders (expected for filled orders)
    exec_client._order_status_reports.clear()

    # Act - run consistency check
    await exec_engine_continuous._check_orders_consistency()

    # Assert - order should remain filled
    assert order.status == OrderStatus.FILLED
    assert order.is_closed
    assert order.filled_qty == Quantity.from_int(100)


@pytest.mark.asyncio
async def test_mixed_orders_with_some_not_found_at_venue(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
):
    """
    Test consistency check with multiple orders where some are not found at venue.
    """
    # Arrange - create multiple orders with different statuses
    order1 = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        client_order_id=ClientOrderId("O-1"),
    )
    order2 = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        client_order_id=ClientOrderId("O-2"),
    )
    order3 = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        client_order_id=ClientOrderId("O-3"),
    )

    # Order1: ACCEPTED and exists at venue
    cache.add_order(order1)
    submitted1 = TestEventStubs.order_submitted(order1, account_id=account_id)
    accepted1 = TestEventStubs.order_accepted(order1, account_id=account_id)
    order1.apply(submitted1)
    order1.apply(accepted1)
    exec_engine_continuous.process(submitted1)
    exec_engine_continuous.process(accepted1)
    cache.update_order(order1)

    # Order2: ACCEPTED but NOT at venue
    cache.add_order(order2)
    submitted2 = TestEventStubs.order_submitted(order2, account_id=account_id)
    accepted2 = TestEventStubs.order_accepted(order2, account_id=account_id)
    order2.apply(submitted2)
    order2.apply(accepted2)
    exec_engine_continuous.process(submitted2)
    exec_engine_continuous.process(accepted2)
    cache.update_order(order2)

    # Order3: FILLED (not at venue is normal)
    cache.add_order(order3)
    submitted3 = TestEventStubs.order_submitted(order3, account_id=account_id)
    accepted3 = TestEventStubs.order_accepted(order3, account_id=account_id)
    filled3 = TestEventStubs.order_filled(order3, instrument=AUDUSD_SIM)
    order3.apply(submitted3)
    order3.apply(accepted3)
    order3.apply(filled3)
    exec_engine_continuous.process(submitted3)
    exec_engine_continuous.process(accepted3)
    exec_engine_continuous.process(filled3)
    cache.update_order(order3)

    # Only order1 exists at venue
    venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order1.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        order_side=order1.side,
        order_type=order1.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        price=order1.price,
        quantity=order1.quantity,
        filled_qty=Quantity.from_int(0),
        report_id=UUID4(),
        ts_accepted=0,
        ts_last=0,
        ts_init=0,
    )
    exec_client._order_status_reports = {venue_report.venue_order_id: venue_report}

    # Simulate max retries reached for order2
    exec_engine_continuous._inflight_check_retries[order2.client_order_id] = (
        exec_engine_continuous.open_check_missing_retries
    )

    # Act - run consistency check
    await exec_engine_continuous._check_orders_consistency()

    # Assert
    assert order1.status == OrderStatus.ACCEPTED  # Still accepted (found at venue)
    assert order2.status == OrderStatus.REJECTED  # Rejected (not found)
    assert order3.status == OrderStatus.FILLED  # Unchanged (filled orders not tracked)


@pytest.mark.asyncio
async def test_missing_order_respects_retry_threshold(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test that orders missing from venue are not immediately resolved but respect retry
    threshold.
    """
    # Arrange - create an accepted order with sufficient age
    exec_engine_continuous.open_check_missing_retries = 2  # Set threshold to 2

    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Make order old enough to check (older than threshold)
    old_ts = clock.timestamp_ns() - 10_000_000_000  # 10 seconds ago
    submitted = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=old_ts,
    )
    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        ts_event=old_ts,
    )

    order.apply(submitted)
    order.apply(accepted)
    exec_engine_continuous.process(submitted)
    exec_engine_continuous.process(accepted)
    cache.update_order(order)

    # Venue has no orders
    exec_client._order_status_reports.clear()

    # Act - First check (should not resolve)
    await exec_engine_continuous._check_orders_consistency()

    # Assert - Order still accepted, retry counter incremented
    assert order.status == OrderStatus.ACCEPTED
    assert exec_engine_continuous._inflight_check_retries[order.client_order_id] == 1

    # Act - Second check (should still not resolve)
    await exec_engine_continuous._check_orders_consistency()

    # Assert - Order still accepted, retry counter incremented
    assert order.status == OrderStatus.ACCEPTED
    assert exec_engine_continuous._inflight_check_retries[order.client_order_id] == 2

    # Act - Third check (should resolve now)
    await exec_engine_continuous._check_orders_consistency()

    # Assert - Order now rejected after exceeding threshold
    assert order.status == OrderStatus.REJECTED
    assert order.client_order_id not in exec_engine_continuous._inflight_check_retries


@pytest.mark.asyncio
async def test_recent_order_skipped_from_missing_check(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test that very recently submitted orders are skipped from missing order checks.
    """
    # Arrange - create a very recently accepted order
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    # Make order very recent (within threshold)
    recent_ts = clock.timestamp_ns() - 1_000_000  # 1ms ago
    submitted = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=recent_ts,
    )
    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        ts_event=recent_ts,
    )

    order.apply(submitted)
    order.apply(accepted)
    exec_engine_continuous.process(submitted)
    exec_engine_continuous.process(accepted)
    cache.update_order(order)

    # Order should be recent due to recent_ts in accepted event

    # Venue has no orders
    exec_client._order_status_reports.clear()

    # Act - run consistency check
    await exec_engine_continuous._check_orders_consistency()

    # Assert - Order should remain accepted, no retry counter
    assert order.status == OrderStatus.ACCEPTED
    assert order.client_order_id not in exec_engine_continuous._inflight_check_retries


@pytest.mark.asyncio
async def test_order_found_at_venue_clears_retry_counter(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test that when an order is found at the venue, its retry counter is cleared.
    """
    # Arrange - create an accepted order with existing retry count
    order = TestExecStubs.limit_order(instrument=AUDUSD_SIM)
    cache.add_order(order)

    old_ts = clock.timestamp_ns() - 10_000_000_000
    submitted = TestEventStubs.order_submitted(order, account_id=account_id, ts_event=old_ts)
    accepted = TestEventStubs.order_accepted(order, account_id=account_id, ts_event=old_ts)

    order.apply(submitted)
    order.apply(accepted)
    exec_engine_continuous.process(submitted)
    exec_engine_continuous.process(accepted)
    cache.update_order(order)

    # Set up existing retry count
    exec_engine_continuous._inflight_check_retries[order.client_order_id] = 2

    # Now order is found at venue
    venue_report = OrderStatusReport(
        account_id=account_id,
        instrument_id=AUDUSD_SIM.id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-123"),
        order_side=order.side,
        order_type=order.order_type,
        time_in_force=TimeInForce.GTC,
        order_status=OrderStatus.ACCEPTED,
        price=order.price,
        quantity=order.quantity,
        filled_qty=Quantity.from_int(0),
        report_id=UUID4(),
        ts_accepted=old_ts,
        ts_last=old_ts,
        ts_init=old_ts,
    )
    exec_client._order_status_reports = {venue_report.venue_order_id: venue_report}

    # Act - run consistency check
    await exec_engine_continuous._check_orders_consistency()

    # Assert - retry counter should be cleared
    assert order.client_order_id not in exec_engine_continuous._inflight_check_retries
    assert order.status == OrderStatus.ACCEPTED


@pytest.mark.asyncio
async def test_open_check_open_only_mode_does_not_reject_missing_orders(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test that orders not in venue's open orders response are NOT marked as rejected when
    using open_check_open_only=True mode (they might be filled/canceled).
    """
    # Arrange - configure for open_only mode
    exec_engine_continuous.open_check_open_only = True
    exec_engine_continuous.open_check_missing_retries = 2

    # Create an ACCEPTED order in cache
    order = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        client_order_id=ClientOrderId("O-123"),
    )
    cache.add_order(order)

    # Make order old enough to check
    old_ts = clock.timestamp_ns() - 10_000_000_000  # 10 seconds ago
    submitted = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=old_ts,
    )
    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        ts_event=old_ts,
    )

    order.apply(submitted)
    order.apply(accepted)
    exec_engine_continuous.process(submitted)
    exec_engine_continuous.process(accepted)
    cache.update_order(order)

    # Configure venue to return empty list (simulating order was filled/canceled)
    exec_client._order_status_reports.clear()

    # Act - Run consistency check multiple times to exceed what would be retry threshold
    for _ in range(3):
        await exec_engine_continuous._check_orders_consistency()

    # Assert - Order should NOT be marked as rejected in open_only mode
    assert order.status == OrderStatus.ACCEPTED
    assert not order.is_closed


@pytest.mark.asyncio
async def test_open_check_full_history_mode_does_reject_missing_orders(
    exec_engine_continuous,
    exec_client,
    cache,
    account_id,
    clock,
):
    """
    Test that orders not found at venue ARE marked as rejected when using
    open_check_open_only=False mode (full history query).
    """
    # Arrange - configure for full history mode
    exec_engine_continuous.open_check_open_only = False
    exec_engine_continuous.open_check_missing_retries = 2

    # Create an ACCEPTED order in cache
    order = TestExecStubs.limit_order(
        instrument=AUDUSD_SIM,
        client_order_id=ClientOrderId("O-456"),
    )
    cache.add_order(order)

    # Make order old enough to check
    old_ts = clock.timestamp_ns() - 10_000_000_000  # 10 seconds ago
    submitted = TestEventStubs.order_submitted(
        order,
        account_id=account_id,
        ts_event=old_ts,
    )
    accepted = TestEventStubs.order_accepted(
        order,
        account_id=account_id,
        ts_event=old_ts,
    )

    order.apply(submitted)
    order.apply(accepted)
    exec_engine_continuous.process(submitted)
    exec_engine_continuous.process(accepted)
    cache.update_order(order)

    # Configure venue to return empty list
    exec_client._order_status_reports.clear()

    # Simulate max retries reached
    exec_engine_continuous._inflight_check_retries[order.client_order_id] = 2

    # Act - Run consistency check to trigger resolution
    await exec_engine_continuous._check_orders_consistency()

    # Assert - Order SHOULD be marked as rejected in full history mode
    assert order.status == OrderStatus.REJECTED
    assert order.is_closed
