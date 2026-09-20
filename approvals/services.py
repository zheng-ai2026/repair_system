"""业务辅助：通知、操作日志、工单历程事件、库存出入库。"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from .models import (
    Notification,
    OperationLog,
    SparePart,
    StockMovement,
    TicketEvent,
    UserProfile,
)


def notify_user(recipient, title, content, *, category=Notification.Category.SYSTEM, business_code=""):
    """给单个用户发通知。"""
    if recipient is None:
        return None
    return Notification.objects.create(
        recipient=recipient,
        category=category,
        title=title,
        content=content,
        business_code=business_code or "",
    )


def notify_role(role, title, content, *, region=None, category=Notification.Category.SYSTEM, business_code=""):
    """给某角色的全部用户发通知；传 region 时限定同片区（含该片区下油站用户）。"""
    profiles = UserProfile.objects.filter(role=role).select_related("user")
    if region is not None:
        profiles = profiles.filter(Q(region=region) | Q(site__region=region))
    notifications = []
    for profile in profiles:
        notifications.append(
            notify_user(
                profile.user, title, content, category=category, business_code=business_code
            )
        )
    return notifications


def log_operation(user, module, action, content, ip, *, result=OperationLog.Result.SUCCESS):
    """写操作日志。"""
    return OperationLog.objects.create(
        user=user if user and user.is_authenticated else None,
        module=module,
        action=action,
        content=content,
        ip=ip or None,
        result=result,
    )


def record_ticket_event(repair, event_type, *, actor=None, detail="", remark=""):
    """追加一条不可变工单历程事件。

    :param repair: 关联维修单
    :param event_type: TicketEvent.EventType
    :param actor: 操作人（未登录/系统操作可传 None）
    :param detail: 环节说明，如「片区管理员审批」「任务派发给 XX 工程队」
    :param remark: 审批意见/完工内容等
    """
    return TicketEvent.objects.create(
        repair_request=repair,
        event_type=event_type,
        actor=actor if actor and actor.is_authenticated else None,
        detail=detail or "",
        remark=remark or "",
    )


def apply_stock_movement(spare_part, movement_type, quantity, *,
                         operator=None, dispatch_order=None, remark=""):
    """库存变更的唯一写入口：行锁 → 校验 → 更新快照 → 落流水（同一事务）。

    :param spare_part: SparePart 实例或主键
    :param movement_type: StockMovement.MovementType.IN / OUT
    :param quantity: 正数数量（Decimal 可接受）
    :returns: 已创建的 StockMovement
    :raises ValueError: 数量非法或出库库存不足（此时不写任何数据）
    """
    try:
        quantity = Decimal(str(quantity)).quantize(Decimal("0.01"))
    except Exception:
        raise ValueError("数量格式不正确")
    if quantity <= 0:
        raise ValueError("出入库数量必须大于 0")
    if movement_type not in StockMovement.MovementType.values:
        raise ValueError("未知的出入库类型")
    if dispatch_order is not None and movement_type != StockMovement.MovementType.OUT:
        raise ValueError("只有出库才能关联派工单")

    with transaction.atomic():
        # 注意：select_for_update 不能搭配可空外键的 select_related（PG 外连接限制）
        part = SparePart.objects.select_for_update().get(
            pk=getattr(spare_part, "pk", spare_part)
        )
        if not part.is_active:
            raise ValueError(f"备件 {part.code} 已停用，不能出入库")

        if movement_type == StockMovement.MovementType.IN:
            balance = part.stock_quantity + quantity
        else:
            if quantity > part.stock_quantity:
                raise ValueError(
                    f"备件 {part.name} 库存不足：当前 {part.stock_quantity}，申请出库 {quantity}"
                )
            balance = part.stock_quantity - quantity

        part.stock_quantity = balance
        part.save(update_fields=["stock_quantity", "updated_at"])
        return StockMovement.objects.create(
            spare_part=part,
            movement_type=movement_type,
            quantity=quantity,
            balance_after=balance,
            dispatch_order=dispatch_order,
            operator=operator if operator and operator.is_authenticated else None,
            remark=remark or "",
        )
