#!/usr/bin/env python3
"""Stuurt een prompt naar de permanente 'coach' tmux-sessie (gestart door
entrypoint.sh) en wacht tot Claude het antwoord naar een bestand heeft
geschreven. Houdt de systeemprompt/tool-cache warm tussen aanroepen — geen
nieuwe agent-opstart-kosten per cron-run, in tegenstelling tot een verse
`claude -p`-subprocess per keer.

Terminal-scherm-tekst parsen (het eerdere ontwerp) bleek fragiel: Claude kan
tussentijds tool-calls doen, lang "nadenken", of de output over meerdere
schermregels laten lopen — elk daarvan brak de eerdere marker/JSON-extractie.
Een bestand laten schrijven en daarop pollen is veel robuuster: het bestand
bestaat pas als Claude klaar is met schrijven."""

import os
import subprocess
import time

SESSION = "coach"
MAX_WAIT_SECONDS = 180
POLL_INTERVAL = 2
FILE_STABLE_POLLS = 2  # aantal identieke bestandsgroottes op rij voordat we "klaar" aannemen


def _tmux(*args: str) -> str:
    result = subprocess.run(["tmux", *args], capture_output=True, text=True, check=True)
    return result.stdout


def _send_literal_and_enter(text: str):
    _tmux("send-keys", "-t", SESSION, "-l", text)
    _tmux("send-keys", "-t", SESSION, "Enter")


def _clear_session():
    """Ruimt de conversatie op zodat de volgende cron-run met een schone lei
    begint — voorkomt onbeperkte contextopbouw in deze langlevende sessie.
    /clear opent een autocomplete-menu; de losse Enter daarna bevestigt het."""
    _send_literal_and_enter("/clear")
    time.sleep(2)
    _tmux("send-keys", "-t", SESSION, "Enter")


def ask_and_wait_for_file(prompt: str, output_file: str):
    if os.path.exists(output_file):
        os.remove(output_file)

    _send_literal_and_enter(prompt)

    deadline = time.time() + MAX_WAIT_SECONDS
    last_size = -1
    stable_count = 0
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        if not os.path.exists(output_file):
            continue
        size = os.path.getsize(output_file)
        if size > 0 and size == last_size:
            stable_count += 1
            if stable_count >= FILE_STABLE_POLLS:
                _clear_session()
                return
        else:
            stable_count = 0
        last_size = size

    _clear_session()
    raise TimeoutError(f"Geen (stabiel) antwoordbestand binnen {MAX_WAIT_SECONDS}s: {output_file}")


if __name__ == "__main__":
    import sys

    ask_and_wait_for_file(sys.stdin.read(), sys.argv[1])
    print("done")
