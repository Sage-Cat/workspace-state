"""A synthetic Chrome whose native windows are exactly the saved ones."""
from copy import deepcopy


def native_profiles(snapshot):
    """Capture reply per profile for a snapshot whose windows Chrome restored unchanged."""
    state = snapshot.get("browsers", {}).get("google_chrome", snapshot.get("chrome", {}))
    result = {}
    runtime = 100
    for profile in state.get("profiles", []):
        windows = []
        for saved in profile.get("windows", []):
            runtime += 1
            window = deepcopy(saved)
            window.pop("id", None)
            window.setdefault("type", "normal")
            window.setdefault("incognito", False)
            window.setdefault("groups", [])
            window["runtime_window_id"] = runtime
            window["runtime_identity"] = {
                "tabs": [[runtime * 100 + index, -1] for index in range(len(window.get("tabs", [])))], "groups": []}
            windows.append(window)
        result[profile["profile"]] = {
            "profile": profile["profile"], "profile_directory": profile.get("profile_directory"),
            "app_id": profile.get("app_id"), "windows": windows}
    return result


def companion(snapshot, other=None):
    """A request_browser replacement that answers capture and reports no claims."""
    native = native_profiles(snapshot)

    def request(action, payload=None, *, profile=None, **_kwargs):
        if action == "capture":
            return deepcopy(native[profile])
        return other(action, payload, profile=profile) if other else {"exists": False}

    return request
