"""VBC (Video Batch Compression): headless server and its clients.

See docs/architecture/service-split.md: the server (``vbc serve``) owns all
state and file operations; ``vbc tui``, ``vbc run`` and the one-shot commands
are clients that talk to it over the HTTP API only.
"""
