"""Lookup core — pure logic with no web framework dependency.

This package must never import FastAPI, uvicorn, or anything from ``app.api``.
That constraint keeps the search rules and the row-to-file matching testable
without a server, and lets ``app/cli.py`` drive them headlessly.
"""
