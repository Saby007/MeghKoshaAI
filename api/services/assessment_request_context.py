from contextvars import ContextVar


dashboard_read: ContextVar[bool] = ContextVar("dashboard_read", default=False)
