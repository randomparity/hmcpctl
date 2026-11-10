"""Bounded CLI escape-hatch probes for the live HMC test harness."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Client

if TYPE_CHECKING:
    from live_test.runner import RunState

# ---------------------------------------------------------------------------
# ST7 — CLI Escape Hatch
# ---------------------------------------------------------------------------


async def exercise_cli_escape_hatch(client: Client, state: RunState) -> None:
    print("\n=== ST7: CLI Escape Hatch ===")

    st, data = await state.call(client, "hmc_run_command", cmd="lshmc -V")
    state.record(7, "hmc_run_command (lshmc -V)", st, data)

    st, data = await state.call(client, "hmc_run_command", cmd="lssyscfg -r sys")
    state.record(7, "hmc_run_command (lssyscfg -r sys)", st, data)
