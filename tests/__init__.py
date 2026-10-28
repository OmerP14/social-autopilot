import os
from pathlib import Path

# Testler kurgusal test firmasıyla çalışır (tests/fixtures/clients/demo); app modüllerinden önce ayarlanır
os.environ["CLIENTS_DIR"] = str(Path(__file__).parent / "fixtures" / "clients")
