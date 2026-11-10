"""`python -m captain_search.trainers <name>`: what a built-in trainer's training command runs."""
import sys

from . import TRAINERS, run

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: python -m captain_search.trainers <{'|'.join(TRAINERS)}>")
    sys.exit(run(sys.argv[1]))
