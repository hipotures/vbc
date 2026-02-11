#!/bin/bash
# Serve VBC documentation locally with live reload

echo "Starting MkDocs development server..."
echo "Documentation will be available at: http://127.0.0.1:8000"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

uv run mkdocs serve
