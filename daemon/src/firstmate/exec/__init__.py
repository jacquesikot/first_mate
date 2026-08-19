"""Execution-layer modules.

Four clean boundaries (PRD §7): tmux control, git operations, context
tracking, hook management. Nothing outside this package touches tmux or
shells out to git.
"""
