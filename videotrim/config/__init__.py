"""Persistent application state, kept under the install root's ``data/``.

Deliberately not under ``venv/``: ``one_click.py --recreate`` deletes the venv,
and credentials, save location and IP history have to survive that.
"""
