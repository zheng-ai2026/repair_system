"""全局模板上下文：顶栏未读通知数。"""


def unread_notification_count(request):
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {"unread_notification_count": 0}
    return {
        "unread_notification_count": request.user.notifications.filter(is_read=False).count()
    }
