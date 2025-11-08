
def log_event(obj):
    try:
        print("[LOG]", obj, flush=True)
    except Exception:
        pass
