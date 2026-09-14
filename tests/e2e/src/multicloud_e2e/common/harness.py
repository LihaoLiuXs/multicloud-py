"""Shared live testing harness utilities.

Provides rich terminal output, unique naming, console URL builders,
interactive pause, step/verify wrappers, timed API calls with spinners,
and cleanup management.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.status import Status
from rich.table import Table

console = Console()


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        console.print(
            f"[red]Config file not found:[/red] {config_path}\n"
            f"Copy config.yaml.example to config.yaml and fill in your values.",
            style="bold",
        )
        raise SystemExit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def unique_name(prefix: str) -> str:
    return f"{prefix}{int(time.time())}"


def pause(interactive: bool, message: str = "Press Enter to continue...") -> None:
    if interactive:
        Confirm.ask(f"  [yellow]│[/yellow] [yellow]{message}[/yellow]", default=True)


def print_header(title: str, subtitle: str = "") -> None:
    content = f"[bold white]{title}[/bold white]"
    if subtitle:
        content += f"\n[dim]{subtitle}[/dim]"
    console.print(Panel(content, border_style="green", padding=(1, 2)))


def print_result(passed: int, total: int, elapsed: float, call_log: "CallLog") -> None:
    if passed == total:
        style = "green"
        icon = "✓"
    else:
        style = "red"
        icon = "✗"
    console.print(
        Panel(
            f"[bold {style}]RESULT: {passed}/{total} steps passed {icon}[/bold {style}]  [dim](elapsed: {elapsed:.1f}s)[/dim]",
            border_style=style,
        )
    )
    if call_log.entries:
        console.print()
        call_log.print_summary()


def step_start(step_num: int, name: str, detail: str = "") -> None:
    console.print(f"\n[bold]▶ STEP {step_num}: {name}[/bold]")
    if detail:
        console.print(f"  [dim]{detail}[/dim]")


def step_success(message: str, elapsed: Optional[float] = None) -> None:
    timing = f" [dim]({elapsed:.1f}s)[/dim]" if elapsed is not None else ""
    console.print(f"  [magenta]│[/magenta] [green]✓[/green] {message}{timing}")


def step_fail(message: str) -> None:
    console.print(f"  [red]│[/red] [red]✗[/red] {message}")


def step_link(url: str) -> None:
    console.print(f"  [yellow]│[/yellow] [blue]🔗[/blue] [link={url}]{url}[/link]")


def section_sdk_call() -> None:
    console.print(f"\n  [bold magenta]┌ MultiCloud SDK Call[/bold magenta]")


def section_provider_verify(provider: str = "Provider") -> None:
    console.print(f"\n  [bold cyan]┌ {provider} SDK Verification[/bold cyan]")


def section_manual_verify() -> None:
    console.print(f"\n  [bold yellow]┌ Manual Verification[/bold yellow]")


def sdk_call_annotation(method: str, **kwargs: Any) -> None:
    """Print SDK call annotation. Pass kwargs for verbose multiline display."""
    if kwargs:
        console.print(
            f"  [magenta]│[/magenta] [magenta]→ MULTICLOUD SDK:[/magenta] {method}"
        )
        for k, v in kwargs.items():
            v_str = str(v)
            if len(v_str) > 80:
                v_str = v_str[:77] + "..."
            console.print(f"  [magenta]│[/magenta]     [dim]{k}=[/dim]{v_str}")
    else:
        console.print(
            f"  [magenta]│[/magenta] [magenta]→ MULTICLOUD SDK:[/magenta] {method}"
        )


def provider_call_annotation(provider: str, method: str, note: str = "") -> None:
    suffix = f" [dim][{note}][/dim]" if note else ""
    console.print(f"  [cyan]│[/cyan] [cyan]→ {provider} SDK:[/cyan] {method}{suffix}")


@dataclass
class TimedResult:
    """Result of a timed call."""

    elapsed: float = 0.0
    value: Any = None


@contextmanager
def timed_call(sdk_method: str):
    """Execute an API call with a spinner and timing.

    Usage:
        with timed_call("IamClient.create_identity()") as t:
            t.value = client.create_identity(...)
        step_success(f"SDK returned: {t.value}", elapsed=t.elapsed)
    """
    result = TimedResult()
    start = time.time()
    with console.status(
        f"[magenta]│[/magenta] [dim]{sdk_method}...[/dim]", spinner="dots"
    ):
        try:
            yield result
        finally:
            result.elapsed = time.time() - start


@contextmanager
def timed_verify(description: str, raw_api: str):
    """Execute a verification call with a spinner and timing.

    Usage:
        with timed_verify("service account exists", "IAMClient.get_service_account()") as t:
            t.value = _verify_sa_exists(...)
    """
    result = TimedResult()
    start = time.time()
    with console.status(f"[dim]Verifying: {raw_api}[/dim]", spinner="dots"):
        try:
            yield result
        finally:
            result.elapsed = time.time() - start


VERIFICATION_DELAY_SECONDS = 5


def step_verify_timed(
    description: str, raw_api: str, check_fn: Callable[[], bool]
) -> tuple:
    """Run verification with spinner, timing, and descriptive output.

    Includes a brief delay before verification to account for eventual
    consistency in cloud provider APIs.

    Returns (passed: bool, elapsed: float).
    """
    start = time.time()
    with console.status(
        f"[cyan]│[/cyan] [dim]Waiting {VERIFICATION_DELAY_SECONDS}s for consistency...[/dim]",
        spinner="dots",
    ):
        time.sleep(VERIFICATION_DELAY_SECONDS)
    with console.status(f"[cyan]│[/cyan] [dim]Verifying...[/dim]", spinner="dots"):
        try:
            result = check_fn()
        except Exception as exc:
            elapsed = time.time() - start
            console.print(
                f"  [cyan]│[/cyan] [red]✗[/red] VERIFICATION ERROR ({description}): {exc}"
            )
            return False, elapsed

    elapsed = time.time() - start
    if result:
        timing = f" [dim]({elapsed:.1f}s)[/dim]"
        console.print(
            f"  [cyan]│[/cyan] [green]✓[/green] VERIFIED: {description}{timing}"
        )
        return True, elapsed
    else:
        console.print(
            f"  [cyan]│[/cyan] [red]✗[/red] VERIFICATION FAILED ({description})"
        )
        return False, elapsed


@dataclass
class CallLogEntry:
    step: str
    sdk_api: str
    provider_api: str
    elapsed: float
    passed: bool
    is_verify: bool = False


class CallLog:
    """Collects API call records for the summary table."""

    def __init__(self) -> None:
        self.entries: List[CallLogEntry] = []

    def record(
        self,
        step: str,
        sdk_api: str,
        provider_api: str,
        elapsed: float,
        passed: bool,
        is_verify: bool = False,
    ) -> None:
        self.entries.append(
            CallLogEntry(
                step=step,
                sdk_api=sdk_api,
                provider_api=provider_api,
                elapsed=elapsed,
                passed=passed,
                is_verify=is_verify,
            )
        )

    def print_summary(self) -> None:
        table = Table(title="API Call Summary", show_lines=False, padding=(0, 1))
        table.add_column("#", style="dim", width=4)
        table.add_column("Step", style="bold")
        table.add_column("MultiCloud SDK API", style="magenta")
        table.add_column("Provider API", style="cyan")
        table.add_column("Time", justify="right")
        table.add_column("", width=2)

        step_num = 0
        for entry in self.entries:
            if not entry.is_verify:
                step_num += 1
            step_label = "  └─ verify" if entry.is_verify else entry.step
            icon = "[green]✓[/green]" if entry.passed else "[red]✗[/red]"
            row_style = "dim" if entry.is_verify else ""
            table.add_row(
                "" if entry.is_verify else str(step_num),
                step_label,
                entry.sdk_api,
                entry.provider_api,
                f"{entry.elapsed:.1f}s",
                icon,
                style=row_style,
            )

        console.print(table)


class CleanupManager:
    """Registers cleanup actions that run even on failure."""

    def __init__(self) -> None:
        self._actions: List[Callable[[], None]] = []

    def register(self, fn: Callable[[], None]) -> None:
        self._actions.append(fn)

    def run_all(self) -> None:
        for action in reversed(self._actions):
            try:
                action()
            except Exception as exc:
                console.print(f"  [yellow]⚠ Cleanup warning:[/yellow] {exc}")


@contextmanager
def lifecycle_test(title: str, subtitle: str = "", interactive: bool = True):
    """Context manager for a complete lifecycle test run."""
    print_header(title, subtitle)
    cleanup = CleanupManager()
    call_log = CallLog()
    start = time.time()
    results = {"passed": 0, "total": 0}

    try:
        yield cleanup, results, call_log
    finally:
        elapsed = time.time() - start
        console.print()
        print_result(results["passed"], results["total"], elapsed, call_log)
        if cleanup._actions:
            console.print("\n[dim]Running cleanup...[/dim]")
            cleanup.run_all()
            console.print("[dim]Cleanup complete.[/dim]")
