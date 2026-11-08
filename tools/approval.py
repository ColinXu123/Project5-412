"""Human-in-the-loop gate for destructive / security-sensitive tools."""
import functools

_grants = set()
_logger = None


def init(logger):
    global _logger
    _logger = logger


def grant(tool_name: str):
    """One-time pre-approval, used after the human approved a specific proposed fix."""
    _grants.add(tool_name)


def clear_grants():
    _grants.clear()


def gated(tool_name: str, fn, why: str):
    @functools.wraps(fn)
    def wrapper(**kwargs):
        if tool_name in _grants:
            _grants.discard(tool_name)
            approved, how = True, "pre-approved fix"
        else:
            print("\n" + "=" * 60)
            print(f"APPROVAL REQUIRED: {tool_name}({kwargs})")
            print(f"Why sensitive: {why}")
            ans = input("Approve? [y/N] ").strip().lower()
            approved, how = ans == "y", "interactive"
        if _logger:
            _logger.log("human", "approval", {"tool": tool_name, "args": kwargs,
                                              "approved": approved, "how": how})
        if not approved:
            return {"status": "denied_by_human", "tool": tool_name}
        return fn(**kwargs)
    return wrapper
