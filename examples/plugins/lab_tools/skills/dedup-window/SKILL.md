---
name: dedup-window
description: How to choose a time window for merging repeated log events. Use before deduplicating event logs by time.
---

# Choosing a deduplication window

Group events by adjacent gaps: an event joins the current group when it follows the
previous event within the window. Measuring from the group's first event gives different
groups for long bursts; say which rule you used.

Start with 60 seconds and report how many events each candidate window merges.
