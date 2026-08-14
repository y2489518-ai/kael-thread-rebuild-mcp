from __future__ import annotations

import argparse
import time
from pathlib import Path

from .config import RebuildConfig
from .coordinator import RebuildCoordinator


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--transcript", type=Path)
    args = parser.parse_args()

    config = RebuildConfig.load(args.config)
    coordinator = RebuildCoordinator(config)
    time.sleep(config.activation_delay_seconds)
    operation = coordinator.state.load(args.operation)
    if operation.get("status") in {"rollback_pending", "rollback_scheduled"}:
        coordinator.perform_rollback(args.operation)
        return 0
    if args.transcript is None:
        raise SystemExit("pending rebuild needs --transcript")
    coordinator.prepare(args.operation, args.transcript)
    coordinator.activate(args.operation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
