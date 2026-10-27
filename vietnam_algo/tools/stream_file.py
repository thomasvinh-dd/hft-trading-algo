import sys
from pathlib import Path
# Stream the file in chunks to bypass the opencode limit
path = Path("/tmp/out.txt")
chunk_size = 4096
with path.open("r") as f:
    while True:
        chunk = f.read(chunk_size)
        if not chunk:
            break
        sys.stdout.write(chunk)
        sys.stdout.flush()
