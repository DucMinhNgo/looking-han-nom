"""Tra cứu Hán-Nôm — searching a finished ground-truth dataset.

    dataset.jsonl + a folder of pictures
        -> [this app: search by link, post id, caption or characters]
        -> the picture, the transcription, and a link to the post

Read-only: both inputs are mounted from the host and neither is ever written.

``app.core`` holds the logic and must stay free of web-framework imports;
``app.api`` is the HTTP adapter; ``app.cli`` drives the same core headlessly.
"""
