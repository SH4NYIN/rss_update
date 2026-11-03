"""Double-click on Windows to start the monitor without a terminal window."""
from rss_update import main

raise SystemExit(main(["start"]))
