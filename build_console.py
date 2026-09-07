"""
Injects backend/console_data.json into ui/console_template.html to produce
ui/console.html. Run backend/export_console_data.py first.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent
data = json.loads((ROOT / "backend" / "console_data.json").read_text(encoding="utf-8"))
compact = json.dumps(data, separators=(",", ":"))
template = (ROOT / "ui" / "console_template.html").read_text(encoding="utf-8")
(ROOT / "ui" / "console.html").write_text(
    template.replace("/*__DATA__*/ null", compact), encoding="utf-8"
)
print("wrote ui/console.html")
