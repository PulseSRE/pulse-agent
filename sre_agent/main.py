"""Interactive CLI for the OpenShift SRE agent."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel

from .agent import SYSTEM_PROMPT, create_client, run_agent_turn_streaming
from .security_agent import SECURITY_SYSTEM_PROMPT, run_security_scan_streaming

console = Console()

# Configure structured audit logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler("/tmp/pulse_agent_audit.log", mode="a")],
)
logging.getLogger("kubernetes").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

MODES: dict[str, dict[str, Any]] = {
    "sre": {
        "banner": ("[bold]OpenShift SRE Agent[/bold]\nAI-powered cluster diagnostics, triage, and operations"),
        "color": "blue",
        "prompt": "sre",
        "runner": run_agent_turn_streaming,
        "base_prompt": SYSTEM_PROMPT,
    },
    "security": {
        "banner": ("[bold]OpenShift Security Scanner[/bold]\nAI-powered cluster security posture assessment"),
        "color": "red",
        "prompt": "sec",
        "runner": run_security_scan_streaming,
        "base_prompt": SECURITY_SYSTEM_PROMPT,
    },
}

HELP_TEXT = """\
[bold]Commands[/bold]
  [cyan]help[/cyan]       Show this help
  [cyan]clear[/cyan]      Reset conversation history
  [cyan]mode[/cyan]       Switch between SRE and Security agents
  [cyan]feedback[/cyan]   Rate the last response (resolved/not) — improves the agent
  [cyan]quit[/cyan]       Exit the agent

[bold]SRE Examples[/bold]
  Check overall cluster health
  Why are pods crashing in namespace monitoring?
  Show me all Warning events across the cluster
  What's the resource utilization across nodes?
  Scale deployment nginx in namespace prod to 5 replicas

[bold]Security Examples[/bold]
  Run a full security audit of the cluster
  Scan pods in namespace default for security issues
  Check RBAC for overly permissive roles
  Which namespaces are missing network policies?
"""


def _confirm_action(tool_name: str, tool_input: dict) -> bool:
    """Prompt the user to confirm a destructive operation."""
    console.print()
    console.print(
        Panel(
            f"[bold yellow]CONFIRM:[/bold yellow] {tool_name}\n[dim]{json.dumps(tool_input, indent=2)}[/dim]",
            border_style="yellow",
            title="Write Operation",
        )
    )
    try:
        answer = console.input("[bold yellow]Proceed? (y/N):[/bold yellow] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def print_banner(mode: str, memory_active: bool):
    cfg = MODES[mode]
    memory_tag = " [dim][memory active][/dim]" if memory_active else ""
    console.print(
        Panel(
            str(cfg["banner"]) + memory_tag + "\n\n[dim]Commands: help, clear, mode, feedback, quit[/dim]",
            border_style=str(cfg["color"]),
        )
    )


def run_repl(mode: str):
    cfg = MODES[mode]

    # Initialize memory system if enabled
    from .memory import MemoryManager, is_memory_enabled

    memory_mgr: MemoryManager | None = None
    if is_memory_enabled():
        memory_mgr = MemoryManager()
        console.print("[dim]Memory system active.[/dim]")

    print_banner(mode, memory_mgr is not None)

    try:
        client = create_client()
    except Exception as e:
        console.print(f"[red]Failed to initialize API client: {e}[/red]")
        sys.exit(1)

    try:
        from .k8s_client import get_core_client

        get_core_client().list_namespace(limit=1)
        console.print("[green]Connected to cluster.[/green]")
    except Exception:
        console.print("[yellow]Warning: Cannot connect to cluster.[/yellow]")
        console.print("[dim]Make sure you are logged in (oc login / kubectl config).[/dim]")

    messages: list[dict] = []
    last_query: str = ""

    while True:
        console.print()
        try:
            user_input = console.input(f"[bold {cfg['color']}]{cfg['prompt']}>[/bold {cfg['color']}] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            console.print("[dim]Goodbye.[/dim]")
            break
        if user_input.lower() == "clear":
            messages.clear()
            console.print("[dim]Conversation cleared.[/dim]")
            continue
        if user_input.lower() == "mode":
            if memory_mgr:
                memory_mgr.close()
            return "switch"
        if user_input.lower() in ("help", "?"):
            console.print(HELP_TEXT)
            continue
        if user_input.lower() == "feedback":
            if memory_mgr and last_query:
                try:
                    answer = console.input("[dim]Was the last issue resolved? (y/n/skip): [/dim]").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    continue
                if answer in ("y", "yes"):
                    result = memory_mgr.update_last_outcome(True)
                    if result:
                        console.print(f"[green]Recorded as resolved (score: {result['score']:.2f})[/green]")
                        if result.get("runbook_id"):
                            console.print("[green]Runbook extracted![/green]")
                elif answer in ("n", "no"):
                    result = memory_mgr.update_last_outcome(False)
                    if result:
                        console.print(f"[yellow]Recorded as unresolved (score: {result['score']:.2f})[/yellow]")
            else:
                console.print("[dim]No previous interaction to rate.[/dim]")
            continue

        messages.append({"role": "user", "content": user_input})
        last_query = user_input

        # Prepare memory-augmented prompt and tools
        augmented_prompt = None
        extra_defs = None
        extra_map = None
        if memory_mgr:
            memory_mgr.start_turn()
            augmented_prompt = memory_mgr.augment_prompt(str(cfg["base_prompt"]), user_input)
            extra_tools = memory_mgr.get_extra_tools()
            extra_defs = [t.to_dict() for t in extra_tools]
            extra_map = {t.name: t for t in extra_tools}

        text_parts: list[str] = []

        def on_text(delta: str):
            text_parts.append(delta)
            console.print(delta, end="", highlight=False)

        def on_tool_use(name: str):
            console.print(f"\n  [dim]> calling {name}...[/dim]")
            if memory_mgr:
                memory_mgr.record_tool_call(name, {})

        try:
            console.print()
            full_response = cfg["runner"](
                client,
                messages,
                system_prompt=augmented_prompt,
                extra_tool_defs=extra_defs,
                extra_tool_map=extra_map,
                on_text=on_text,
                on_tool_use=on_tool_use,
                on_confirm=_confirm_action,
            )
            console.print()
        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted.[/dim]")
            messages.pop()
            continue
        except Exception as e:
            console.print(f"\n[red]Error: {type(e).__name__}: {e}[/red]")
            messages.pop()
            continue

        if full_response:
            messages.append({"role": "assistant", "content": full_response})
            # Record interaction in memory
            if memory_mgr:
                result = memory_mgr.finish_turn(user_input, full_response)
                console.print(f"[dim]Memory: score={result['score']:.2f}[/dim]")
        else:
            console.print("[dim]No response.[/dim]")
            messages.pop()

    if memory_mgr:
        memory_mgr.close()
    return "quit"


def main():
    from .logging_config import configure_logging

    configure_logging()

    mode = "sre"
    if len(sys.argv) > 1 and sys.argv[1] in MODES:
        mode = sys.argv[1]

    while True:
        result = run_repl(mode)
        if result == "switch":
            mode = "security" if mode == "sre" else "sre"
            continue
        break


if __name__ == "__main__":
    main()
