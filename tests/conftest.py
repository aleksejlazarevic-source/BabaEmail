import sys
from pathlib import Path

# Make `import babaemail` work when pytest is run as a bare command.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
