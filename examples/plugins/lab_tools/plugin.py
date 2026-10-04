"""An example plugin: log-deduplication helpers for a lab.

The directory itself is the plugin. Besides this file it holds a skill (skills/), a prompt
template (prompts/) and a subagent definition (agents/), which load by convention.
"""

from .ops import count_duplicates

__version__ = "0.1.0"


def setup(api):
    api.add_tool(count_duplicates)
    lab = api.options.get("lab", "the lab")
    api.add_system_prompt(f"You work for {lab}. Never delete raw log files.")

    @api.on("before_tool_call")
    def keep_raw_data(call, args, context):
        # A rule enforced in code, not only stated in the prompt.
        if call.name == "delete_file" and str(args.get("path", "")).startswith("raw/"):
            return False
        return None

    api.add_check(lambda: count_duplicates(["a", "b", "a"]) == 1, "counts_duplicates")
