import io

import qrcode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    AcceptanceRatingForm,
    DispatchCompleteForm,
    DispatchCreateForm,
    DispatchReassignForm,
    DispatchRejectForm,
    EquipmentForm,
    RepairMaterialItemFormSet,
    RepairRequestForm,
    SettlementCreateForm,
    SparePartForm,
    StockInForm,
    StockOutForm,
    can_manage_equipment,
    visible_equipments,
    visible_repair_requests,
)
from .models import (
    AcceptanceRating,
    ApprovalRecord,
    CommonPhrase,
    DispatchMedia,
    DispatchOrder,
    Equipment,
    RepairMedia,
    RepairRequest,
    SettlementOrder,
    SparePart,
    StockMovement,
    TicketEvent,
    UserProfile,
)
from .services import (
    apply_stock_movement,
    log_operation,
    notify_role,
    notify_user,
    record_ticket_event,
)


def _entry_status(repair):
    """单据提交/重新提交时的首个审批状态。

    特急（critical）走快速通道：跳过片区管理员汇总、片区经理复核两级，
    直接进入安数部维修岗审批，其余审批环节不变。
    """
    if repair.urgency == RepairRequest.Urgency.CRITICAL:
        return RepairRequest.Status.PENDING_SAFETY_REPAIR
    return RepairRequest.Status.PENDING_REGION_ADMIN


def _validate_media_uploads(uploads, media_model):
    """校验多文件上传，返回 [(uploaded_file, media_type, size_limit)]。

    任一文件类型不支持或超限时返回 (None, 错误信息)。
    """
    rows = []
    for upload in uploads:
        media_type = media_model.detect_type(upload.name)
        if media_type is None:
            return None, f"不支持的文件类型：{upload.name}（仅支持图片和视频）"
        size_limit = (
            media_model.MAX_IMAGE_SIZE
            if media_type == media_model.MediaType.IMAGE
            else media_model.MAX_VIDEO_SIZE
        )
        if upload.size > size_limit:
            limit_mb = size_limit // (1024 * 1024)
            return None, f"文件 {upload.name} 超过大小限制（{'照片' if media_type == media_model.MediaType.IMAGE else '视频'} ≤{limit_mb}MB）"
        rows.append((upload, media_type, size_limit))
    return rows, None

# 角色 -> 该角色负责审批的维修单状态
ROLE_PENDING_STATUS = {
    UserProfile.Role.REGION_ADMIN: RepairRequest.Status.PENDING_REGION_ADMIN,
    UserProfile.Role.REGION_MANAGER: RepairRequest.Status.PENDING_REGION_MANAGER,
    UserProfile.Role.SAFETY_REPAIR: RepairRequest.Status.PENDING_SAFETY_REPAIR,
    UserProfile.Role.DEPUTY_GM: RepairRequest.Status.PENDING_DEPUTY_GM,
    UserProfile.Role.GM: RepairRequest.Status.PENDING_GM,
    UserProfile.Role.CHAIRMAN: RepairRequest.Status.PENDING_CHAIRMAN,
}

# 审批通过后状态推进
NEXT_STATUS = {
    RepairRequest.Status.PENDING_REGION_ADMIN: RepairRequest.Status.PENDING_REGION_MANAGER,
    RepairRequest.Status.PENDING_REGION_MANAGER: RepairRequest.Status.PENDING_SAFETY_REPAIR,
    RepairRequest.Status.PENDING_SAFETY_REPAIR: RepairRequest.Status.PENDING_DEPUTY_GM,
    RepairRequest.Status.PENDING_DEPUTY_GM: RepairRequest.Status.PENDING_GM,
    RepairRequest.Status.PENDING_GM: RepairRequest.Status.PENDING_CHAIRMAN,
    RepairRequest.Status.PENDING_CHAIRMAN: RepairRequest.Status.APPROVED,
}

# 下一审批环节对应的角色
NEXT_STATUS_ROLE = {
    RepairRequest.Status.PENDING_REGION_MANAGER: UserProfile.Role.REGION_MANAGER,
    RepairRequest.Status.PENDING_SAFETY_REPAIR: UserProfile.Role.SAFETY_REPAIR,
    RepairRequest.Status.PENDING_DEPUTY_GM: UserProfile.Role.DEPUTY_GM,
    RepairRequest.Status.PENDING_GM: UserProfile.Role.GM,
    RepairRequest.Status.PENDING_CHAIRMAN: UserProfile.Role.CHAIRMAN,
}


def approvable_statuses(user):
    """当前用户有权审批的状态集合。"""
    if user.is_superuser:
        return set(NEXT_STATUS.keys())
    profile = getattr(user, "profile", None)
    if profile and profile.role in ROLE_PENDING_STATUS:
        return {ROLE_PENDING_STATUS[profile.role]}
    return set()


@login_required
def home(request):
    user = request.user
    visible = visible_repair_requests(user)
    context = {
        "my_submitted": visible.filter(reporter=user).count(),
        "in_approval": visible.filter(
            status__in=[
                RepairRequest.Status.PENDING_REGION_ADMIN,
                RepairRequest.Status.PENDING_REGION_MANAGER,
                RepairRequest.Status.PENDING_SAFETY_REPAIR,
                RepairRequest.Status.PENDING_DEPUTY_GM,
                RepairRequest.Status.PENDING_GM,
                RepairRequest.Status.PENDING_CHAIRMAN,
            ]
        ).count(),
        "accepted": visible.filter(
            status__in=[RepairRequest.Status.APPROVED, RepairRequest.Status.ACCEPTING]
        ).count(),
        "settled": visible.filter(status=RepairRequest.Status.SETTLED).count(),
        "pending_for_me": 0,
        "pending_status": None,
        "my_dispatch_in_progress": 0,
        "my_dispatch_pending_accept": 0,
        "low_stock_count": 0,
    }

    # 当前用户作为审批人的待办数量
    if user.is_superuser:
        context["pending_for_me"] = visible.exclude(
            status__in=[
                RepairRequest.Status.DRAFT,
                RepairRequest.Status.APPROVED,
                RepairRequest.Status.ACCEPTING,
                RepairRequest.Status.SETTLED,
            ]
        ).count()
        context["low_stock_count"] = SparePart.objects.filter(
            is_active=True, stock_quantity__lte=F("safety_stock")
        ).count()
    else:
        profile = getattr(user, "profile", None)
        if profile and profile.role in ROLE_PENDING_STATUS:
            pending_status = ROLE_PENDING_STATUS[profile.role]
            context["pending_for_me"] = visible.filter(status=pending_status).count()
            context["pending_status"] = pending_status.value

        # 工程队负责人：自己工程队进行中的派工单
        if profile and profile.role == UserProfile.Role.TEAM_LEADER:
            team = getattr(user, "engineering_team", None)
            if team is not None:
                context["my_dispatch_in_progress"] = team.dispatch_orders.exclude(
                    status=DispatchOrder.Status.CLOSED
                ).count()
                context["my_dispatch_pending_accept"] = team.dispatch_orders.filter(
                    status=DispatchOrder.Status.PENDING_ACCEPT
                ).count()

        # 安数部维修岗：低库存备件数量
        if profile and profile.role == UserProfile.Role.SAFETY_REPAIR:
            context["low_stock_count"] = SparePart.objects.filter(
                is_active=True, stock_quantity__lte=F("safety_stock")
            ).count()

    return render(request, "approvals/home.html", context)


@login_required
def repair_list(request):
    repairs = visible_repair_requests(request.user)

    status = request.GET.get("status", "")
    keyword = request.GET.get("q", "").strip()
    if status:
        repairs = repairs.filter(status=status)
    if keyword:
        repairs = repairs.filter(
            Q(code__icontains=keyword)
            | Q(equipment_name__icontains=keyword)
            | Q(site__name__icontains=keyword)
        )

    repairs = repairs.order_by("-created_at")[:200]
    return render(
        request,
        "approvals/repair_list.html",
        {
            "repairs": repairs,
            "status_choices": RepairRequest.Status.choices,
            "current_status": status,
            "keyword": keyword,
        },
    )


@login_required
def repair_add(request):
    if request.method == "POST":
        form = RepairRequestForm(request.POST, request.FILES, user=request.user)
        formset = RepairMaterialItemFormSet(request.POST, queryset=RepairRequest.objects.none())
        uploads = request.FILES.getlist("media")
        # 媒体文件先校验：任何一个不合法则整单不落库
        media_rows, media_error = _validate_media_uploads(uploads, RepairMedia)
        if media_error:
            messages.error(request, media_error)
        elif form.is_valid() and formset.is_valid():
            with transaction.atomic():
                repair = form.save(commit=False)
                repair.reporter = request.user
                repair.status = _entry_status(repair)
                # 选择了设备档案但未手填设备名称时，自动带入设备名称
                if repair.equipment_id and not repair.equipment_name.strip():
                    repair.equipment_name = repair.equipment.name
                repair.save()
                if repair.equipment_id:
                    repair.equipment.refresh_status()
                for item_form in formset:
                    # 未改动的空行 / 勾选删除 / 自识别空行：跳过
                    if (
                        not item_form.has_changed()
                        or item_form.cleaned_data.get("DELETE")
                        or item_form.cleaned_data.get("_empty")
                    ):
                        continue
                    item = item_form.save(commit=False)
                    item.repair_request = repair
                    item.save()
                for upload, media_type, _size_limit in media_rows:
                    RepairMedia.objects.create(
                        repair_request=repair,
                        media_type=media_type,
                        file=upload,
                        filename=upload.name,
                        uploaded_by=request.user,
                    )
                is_fast_track = repair.status == RepairRequest.Status.PENDING_SAFETY_REPAIR
                record_ticket_event(
                    repair,
                    TicketEvent.EventType.CREATED,
                    actor=request.user,
                    detail="特急快速通道：直达安数部维修岗" if is_fast_track else "提交报修，进入片区管理员汇总",
                )
            log_operation(
                request.user, "维修单", "提交",
                f"提交维修单 {repair.code}（{repair.site.name}）"
                f"{'，特急快速通道' if is_fast_track else ''}，附件 {len(media_rows)} 个",
                request.META.get("REMOTE_ADDR"),
            )
            if is_fast_track:
                notify_role(
                    UserProfile.Role.SAFETY_REPAIR,
                    f"【特急】维修单待审批：{repair.code}",
                    f"{repair.site.name} 提交特急维修单「{repair.equipment_name or repair.repair_type}」，"
                    f"已走快速通道直达，请立即处理。",
                    category="approval",
                    business_code=repair.code,
                )
                messages.success(
                    request,
                    f"特急维修单 {repair.code} 已提交，已走快速通道直达安数部维修岗审批。",
                )
            else:
                notify_role(
                    UserProfile.Role.REGION_ADMIN,
                    f"新维修单待汇总：{repair.code}",
                    f"{repair.site.name} 提交了维修单「{repair.equipment_name or repair.repair_type}」，请及时汇总。",
                    region=repair.site.region,
                    category="approval",
                    business_code=repair.code,
                )
                messages.success(request, f"维修单 {repair.code} 已提交，等待片区管理员汇总。")
            return redirect("repair_detail", pk=repair.pk)
    else:
        form = RepairRequestForm(user=request.user)
        formset = RepairMaterialItemFormSet(queryset=RepairRequest.objects.none())
        # 扫码报修：?equipment=<pk> 自动选中设备并定位油站
        equipment_id = request.GET.get("equipment", "")
        if equipment_id:
            equipment = form.fields["equipment"].queryset.filter(pk=equipment_id).first()
            if equipment is not None:
                form.fields["equipment"].initial = equipment.pk
                form.fields["site"].initial = equipment.site_id
                if not form.fields["equipment_name"].initial:
                    form.fields["equipment_name"].initial = equipment.name

    return render(
        request,
        "approvals/repair_form.html",
        {"form": form, "formset": formset},
    )


@login_required
def repair_detail(request, pk):
    repair = get_object_or_404(
        visible_repair_requests(request.user),
        pk=pk,
    )
    material_items = repair.material_items.select_related("material")
    approvals = repair.approval_records.select_related("approver")
    media_files = list(repair.media_files.all())
    events = repair.events.select_related("actor")
    dispatch = getattr(repair, "dispatch_order", None)
    settlement = dispatch.settlement_orders.first() if dispatch else None
    can_approve = repair.status in approvable_statuses(request.user)
    phrases = CommonPhrase.objects.filter(is_active=True).filter(
        Q(node="") | Q(node=ApprovalRecord.Node.APPLICATION)
    ).order_by("sort_order", "id")
    can_resubmit = (
        repair.status == RepairRequest.Status.DRAFT and repair.reporter_id == request.user.id
    )
    return render(
        request,
        "approvals/repair_detail.html",
        {
            "repair": repair,
            "material_items": material_items,
            "approvals": approvals,
            "media_files": media_files,
            "events": events,
            "dispatch": dispatch,
            "settlement": settlement,
            "material_total": sum((i.subtotal for i in material_items), 0),
            "can_approve": can_approve,
            "phrases": phrases,
            "can_resubmit": can_resubmit,
        },
    )


@login_required
def approval_queue(request):
    statuses = approvable_statuses(request.user)
    repairs = (
        visible_repair_requests(request.user)
        .filter(status__in=statuses)
        .order_by("urgency", "-created_at")
        if statuses
        else RepairRequest.objects.none()
    )
    return render(
        request,
        "approvals/approval_queue.html",
        {"repairs": repairs, "is_approver": bool(statuses)},
    )


@login_required
@require_POST
def approve(request, pk):
    repair = get_object_or_404(visible_repair_requests(request.user), pk=pk)
    if repair.status not in approvable_statuses(request.user):
        messages.error(request, "当前单据不在你的审批环节或已被处理。")
        return redirect("repair_detail", pk=pk)

    result = request.POST.get("result")
    comment = request.POST.get("comment", "").strip()
    if result not in (ApprovalRecord.Result.APPROVED, ApprovalRecord.Result.REJECTED):
        messages.error(request, "无效的审批结果。")
        return redirect("repair_detail", pk=pk)

    current_status = repair.status
    step_label = repair.get_status_display()
    ApprovalRecord.objects.create(
        repair_request=repair,
        approver=request.user,
        node=ApprovalRecord.Node.APPLICATION,
        step=current_status,
        result=result,
        comment=comment,
    )

    if result == ApprovalRecord.Result.APPROVED:
        next_status = NEXT_STATUS[current_status]
        repair.status = next_status
        repair.save(update_fields=["status"])

        if next_status == RepairRequest.Status.APPROVED:
            record_ticket_event(
                repair,
                TicketEvent.EventType.APPROVED,
                actor=request.user,
                detail=f"环节「{step_label}」通过，全部审批完成",
                remark=comment,
            )
            notify_user(
                repair.reporter,
                f"维修单 {repair.code} 审批完成",
                "全部审批环节已通过，等待派工。",
                category="approval",
                business_code=repair.code,
            )
            messages.success(request, "已通过：该维修单全部审批环节完成。")
        else:
            record_ticket_event(
                repair,
                TicketEvent.EventType.APPROVED,
                actor=request.user,
                detail=f"环节「{step_label}」通过，进入下一环节",
                remark=comment,
            )
            next_role = NEXT_STATUS_ROLE[next_status]
            notify_role(
                next_role,
                f"新维修单待审批：{repair.code}",
                f"维修单 {repair.code} 已流转到你处，请及时审批。",
                region=repair.site.region,
                category="approval",
                business_code=repair.code,
            )
            notify_user(
                repair.reporter,
                f"维修单 {repair.code} 已通过当前环节",
                f"审批人 {request.user.username} 通过，进入下一审批环节。",
                category="approval",
                business_code=repair.code,
            )
            messages.success(request, "已通过，单据进入下一审批环节。")
        log_operation(
            request.user, "维修审批", "通过",
            f"{repair.code} 环节「{step_label}」通过；意见：{comment or '无'}",
            request.META.get("REMOTE_ADDR"),
        )
    else:
        repair.status = RepairRequest.Status.DRAFT
        repair.save(update_fields=["status"])
        record_ticket_event(
            repair,
            TicketEvent.EventType.REJECTED,
            actor=request.user,
            detail=f"环节「{step_label}」驳回，退回申请人草稿",
            remark=comment,
        )
        notify_user(
            repair.reporter,
            f"维修单 {repair.code} 被驳回",
            f"审批人 {request.user.username} 驳回并退回草稿。意见：{comment or '无'}",
            category="approval",
            business_code=repair.code,
        )
        messages.warning(request, "已驳回，单据退回申请人草稿。")
        log_operation(
            request.user, "维修审批", "驳回",
            f"{repair.code} 环节「{step_label}」驳回；意见：{comment or '无'}",
            request.META.get("REMOTE_ADDR"),
        )

    return redirect("repair_detail", pk=pk)


@login_required
@require_POST
def resubmit(request, pk):
    repair = get_object_or_404(visible_repair_requests(request.user), pk=pk)
    if repair.status != RepairRequest.Status.DRAFT or repair.reporter_id != request.user.id:
        messages.error(request, "只有草稿状态的申请人可以重新提交。")
        return redirect("repair_detail", pk=pk)

    repair.status = _entry_status(repair)
    repair.save(update_fields=["status"])
    is_fast_track = repair.status == RepairRequest.Status.PENDING_SAFETY_REPAIR
    if is_fast_track:
        notify_role(
            UserProfile.Role.SAFETY_REPAIR,
            f"【特急】维修单重新提交：{repair.code}",
            f"{repair.site.name} 的特急维修单 {repair.code} 修改后重新提交，已走快速通道，请立即处理。",
            category="approval",
            business_code=repair.code,
        )
    else:
        notify_role(
            UserProfile.Role.REGION_ADMIN,
            f"维修单重新提交：{repair.code}",
            f"{repair.site.name} 的维修单 {repair.code} 修改后重新提交，请汇总。",
            region=repair.site.region,
            category="approval",
            business_code=repair.code,
        )
    record_ticket_event(
        repair,
        TicketEvent.EventType.RESUBMITTED,
        actor=request.user,
        detail="特急快速通道重新提交，直达安数部维修岗" if is_fast_track else "驳回后重新提交，进入片区管理员汇总",
    )
    log_operation(
        request.user, "维修单", "重新提交",
        (
            f"维修单 {repair.code} 驳回后重新提交（特急快速通道）"
            if is_fast_track
            else f"维修单 {repair.code} 驳回后重新提交"
        ),
        request.META.get("REMOTE_ADDR"),
    )
    messages.success(
        request,
        "已重新提交，特急单直达安数部维修岗。" if is_fast_track else "已重新提交，等待片区管理员汇总。",
    )
    return redirect("repair_detail", pk=pk)


@login_required
def notification_list(request):
    qs = request.user.notifications.all()
    unread = qs.filter(is_read=False)
    if request.method == "POST":
        unread.update(is_read=True, read_at=timezone.now())
        messages.success(request, "全部通知已标记为已读。")
        return redirect("notification_list")
    return render(
        request,
        "approvals/notification_list.html",
        {
            "notifications": qs[:100],
            "unread_count": unread.count(),
        },
    )


# ===================== 设备档案（一物一码） =====================

@login_required
def equipment_list(request):
    equipments = visible_equipments(request.user)
    category = request.GET.get("category", "")
    status = request.GET.get("status", "")
    keyword = request.GET.get("q", "").strip()
    if category:
        equipments = equipments.filter(category=category)
    if status:
        equipments = equipments.filter(status=status)
    if keyword:
        equipments = equipments.filter(
            Q(code__icontains=keyword)
            | Q(name__icontains=keyword)
            | Q(serial_number__icontains=keyword)
            | Q(site__name__icontains=keyword)
            | Q(location__icontains=keyword)
        )
    today = timezone.localdate()
    equipments = equipments.order_by("site__code", "code")[:300]
    return render(
        request,
        "approvals/equipment_list.html",
        {
            "equipments": equipments,
            "category_choices": Equipment.Category.choices,
            "status_choices": Equipment.Status.choices,
            "current_category": category,
            "current_status": status,
            "keyword": keyword,
            "today": today,
            "can_manage": can_manage_equipment(request.user),
        },
    )


@login_required
def equipment_detail(request, pk):
    equipment = get_object_or_404(visible_equipments(request.user), pk=pk)
    history = (
        equipment.repair_requests
        .select_related("reporter", "dispatch_order", "dispatch_order__engineering_team")
        .order_by("-created_at")
    )
    return render(
        request,
        "approvals/equipment_detail.html",
        {
            "equipment": equipment,
            "history": history,
            "expiry_alerts": equipment.expiry_alerts(),
            "can_manage": can_manage_equipment(request.user, equipment),
        },
    )


@login_required
def equipment_add(request):
    if not can_manage_equipment(request.user):
        messages.error(request, "你没有新增设备档案的权限。")
        return redirect("equipment_list")
    if request.method == "POST":
        form = EquipmentForm(request.POST, user=request.user)
        if form.is_valid():
            equipment = form.save()
            log_operation(
                request.user, "设备档案", "新增",
                f"新增设备 {equipment.code}（{equipment.site.name} - {equipment.name}）",
                request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, f"设备档案 {equipment.code} 已建立，可打印二维码张贴。")
            return redirect("equipment_detail", pk=equipment.pk)
    else:
        form = EquipmentForm(user=request.user)
    return render(request, "approvals/equipment_form.html", {"form": form, "is_add": True})


@login_required
def equipment_edit(request, pk):
    equipment = get_object_or_404(visible_equipments(request.user), pk=pk)
    if not can_manage_equipment(request.user, equipment):
        messages.error(request, "你没有编辑该设备档案的权限。")
        return redirect("equipment_detail", pk=pk)
    if request.method == "POST":
        form = EquipmentForm(request.POST, instance=equipment, user=request.user)
        if form.is_valid():
            form.save()
            log_operation(
                request.user, "设备档案", "编辑",
                f"编辑设备 {equipment.code}（{equipment.name}），状态：{equipment.get_status_display()}",
                request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, "设备档案已更新。")
            return redirect("equipment_detail", pk=equipment.pk)
    else:
        form = EquipmentForm(instance=equipment, user=request.user)
    return render(
        request,
        "approvals/equipment_form.html",
        {"form": form, "equipment": equipment, "is_add": False},
    )


@login_required
def equipment_qr_image(request, pk):
    """动态生成设备二维码 PNG（内容为设备详情页绝对 URL，手机扫码即达）。"""
    equipment = get_object_or_404(visible_equipments(request.user), pk=pk)
    target_url = request.build_absolute_uri(
        reverse("equipment_detail", args=[equipment.pk])
    )
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(target_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Cache-Control"] = "no-store"
    return response


@login_required
def equipment_qr_page(request, pk):
    """二维码打印页：打印后张贴到设备现场。"""
    equipment = get_object_or_404(visible_equipments(request.user), pk=pk)
    return render(request, "approvals/equipment_qr.html", {"equipment": equipment})


# ===================== 派工、验收与结算 =====================

def visible_dispatch_orders(user):
    """按角色返回可见派工单。"""
    qs = DispatchOrder.objects.select_related(
        "repair_request__site", "engineering_team", "leader"
    )
    if user.is_superuser:
        return qs
    profile = getattr(user, "profile", None)
    if profile is None:
        return qs.none()
    role = profile.role
    if role in (
        UserProfile.Role.SAFETY_REPAIR,
        UserProfile.Role.DEPUTY_GM,
        UserProfile.Role.GM,
        UserProfile.Role.CHAIRMAN,
    ):
        return qs
    if role in (UserProfile.Role.REGION_ADMIN, UserProfile.Role.REGION_MANAGER):
        if profile.region_id:
            return qs.filter(repair_request__site__region_id=profile.region_id)
        return qs.none()
    if role == UserProfile.Role.SITE_MANAGER:
        if profile.site_id:
            return qs.filter(repair_request__site_id=profile.site_id)
        return qs.none()
    if role == UserProfile.Role.TEAM_LEADER:
        team = getattr(user, "engineering_team", None)
        if team is not None:
            return qs.filter(engineering_team=team)
        return qs.none()
    return qs.none()


def _is_safety(user):
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role == UserProfile.Role.SAFETY_REPAIR)


def _is_region_admin(user):
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role == UserProfile.Role.REGION_ADMIN)


def _is_team_member(user, dispatch):
    if user.is_superuser:
        return True
    if dispatch.leader_id == user.id:
        return True
    team = getattr(user, "engineering_team", None)
    return team is not None and team.pk == dispatch.engineering_team_id


@login_required
def dispatch_list(request):
    orders = visible_dispatch_orders(request.user).order_by("-created_at")[:200]
    can_create = _is_safety(request.user)
    return render(
        request,
        "approvals/dispatch_list.html",
        {"orders": orders, "can_create": can_create},
    )


@login_required
def dispatch_create(request):
    if not _is_safety(request.user):
        messages.error(request, "只有安数部维修岗可以派工。")
        return redirect("dispatch_list")

    if request.method == "POST":
        form = DispatchCreateForm(request.POST, user=request.user)
        if form.is_valid():
            repair = form.cleaned_data["repair_request"]
            team = form.cleaned_data["engineering_team"]
            dispatch = DispatchOrder.objects.create(
                repair_request=repair,
                engineering_team=team,
                leader=form.cleaned_data.get("leader") or getattr(team, "bound_user", None),
                content=form.cleaned_data.get("content", ""),
                status=DispatchOrder.Status.PENDING_ACCEPT,
            )
            repair.status = RepairRequest.Status.ACCEPTING
            repair.save(update_fields=["status"])
            if repair.equipment_id:
                repair.equipment.refresh_status()

            recipients = [u for u in (team.bound_user, dispatch.leader) if u]
            for u in recipients:
                notify_user(
                    u,
                    f"新派工任务待接单：{dispatch.code}",
                    f"维修单 {repair.code}（{repair.site.name}）已派给你所在的工程队，请尽快接单或说明拒单理由。",
                    category="dispatch",
                    business_code=dispatch.code,
                )
            notify_user(
                repair.reporter,
                f"维修单 {repair.code} 已派工",
                f"已派发给工程队「{team.full_name}」，等待对方接单。",
                category="dispatch",
                business_code=dispatch.code,
            )
            log_operation(
                request.user, "派工", "创建派工单",
                f"派工单 {dispatch.code} -> {team.full_name}（维修单 {repair.code}）",
                request.META.get("REMOTE_ADDR"),
            )
            record_ticket_event(
                repair,
                TicketEvent.EventType.DISPATCHED,
                actor=request.user,
                detail=f"任务派发给工程队「{team.full_name}」，派工单 {dispatch.code}，等待接单",
            )
            messages.success(request, f"派工单 {dispatch.code} 已创建，等待工程队接单。")
            return redirect("dispatch_detail", pk=dispatch.pk)
    else:
        form = DispatchCreateForm(user=request.user)
    return render(request, "approvals/dispatch_form.html", {"form": form})


@login_required
def dispatch_detail(request, pk):
    dispatch = get_object_or_404(visible_dispatch_orders(request.user), pk=pk)
    settlement = dispatch.settlement_orders.first()
    is_team_member = _is_team_member(request.user, dispatch)
    can_accept = (
        dispatch.status == DispatchOrder.Status.PENDING_ACCEPT and is_team_member
    )
    can_complete = (
        dispatch.status == DispatchOrder.Status.IN_PROGRESS and is_team_member
    )
    can_admin_review = (
        dispatch.status == DispatchOrder.Status.ACCEPTING and _is_region_admin(request.user)
    )
    can_final_review = (
        dispatch.status == DispatchOrder.Status.ADMIN_REVIEW and _is_safety(request.user)
    )
    can_reassign = (
        dispatch.status == DispatchOrder.Status.REJECTED and _is_safety(request.user)
    )
    can_settle = (
        dispatch.status == DispatchOrder.Status.CLOSED
        and settlement is None
        and _is_safety(request.user)
    )
    can_confirm_settlement = (
        settlement is not None and not settlement.is_settled and _is_safety(request.user)
    )
    rating = getattr(dispatch, "rating", None)
    # 报修人在完工上报后可评价（验收中各环节及闭环后），且只能评价一次
    can_rate = (
        dispatch.status
        in (
            DispatchOrder.Status.ACCEPTING,
            DispatchOrder.Status.ADMIN_REVIEW,
            DispatchOrder.Status.SAFETY_REVIEW,
            DispatchOrder.Status.CLOSED,
        )
        and dispatch.repair_request.reporter_id == request.user.id
        and rating is None
    )
    media_before = [m for m in dispatch.media_files.all() if m.phase == DispatchMedia.Phase.BEFORE]
    media_after = [m for m in dispatch.media_files.all() if m.phase != DispatchMedia.Phase.BEFORE]
    return render(
        request,
        "approvals/dispatch_detail.html",
        {
            "dispatch": dispatch,
            "settlement": settlement,
            "media_before": media_before,
            "media_after": media_after,
            "complete_form": DispatchCompleteForm(),
            "reject_form": DispatchRejectForm(),
            "reassign_form": DispatchReassignForm(),
            "rating_form": AcceptanceRatingForm(),
            "settlement_form": SettlementCreateForm(),
            "rating": rating,
            "can_accept": can_accept,
            "can_complete": can_complete,
            "can_admin_review": can_admin_review,
            "can_final_review": can_final_review,
            "can_reassign": can_reassign,
            "can_settle": can_settle,
            "can_confirm_settlement": can_confirm_settlement,
            "can_rate": can_rate,
        },
    )


@login_required
@require_POST
def dispatch_complete(request, pk):
    dispatch = get_object_or_404(visible_dispatch_orders(request.user), pk=pk)
    if dispatch.status != DispatchOrder.Status.IN_PROGRESS or not _is_team_member(
        request.user, dispatch
    ):
        messages.error(request, "当前状态不允许完工上报。")
        return redirect("dispatch_detail", pk=pk)

    form = DispatchCompleteForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "完工上报信息有误：" + form.errors.as_text())
        return redirect("dispatch_detail", pk=pk)

    uploads_after = request.FILES.getlist("media")
    uploads_before = request.FILES.getlist("before_media")
    if not uploads_after:
        messages.error(request, "请至少上传一张维修后照片或一个视频后再提交完工。")
        return redirect("dispatch_detail", pk=pk)
    after_rows, media_error = _validate_media_uploads(uploads_after, DispatchMedia)
    if media_error:
        messages.error(request, media_error)
        return redirect("dispatch_detail", pk=pk)
    before_rows, media_error = _validate_media_uploads(uploads_before, DispatchMedia)
    if media_error:
        messages.error(request, media_error)
        return redirect("dispatch_detail", pk=pk)

    cleaned = form.cleaned_data
    if cleaned.get("content"):
        dispatch.content = cleaned["content"]
    dispatch.actual_fault = cleaned.get("actual_fault") or ""
    dispatch.repair_plan = cleaned.get("repair_plan") or ""
    dispatch.actual_measures = cleaned.get("actual_measures") or ""
    dispatch.work_hours = cleaned.get("work_hours")
    # 第一张维修后照片同步写入旧的单图字段，兼容既有展示与后台
    first_image = next(
        (u for u, t, _limit in after_rows if t == DispatchMedia.MediaType.IMAGE), None
    )
    if first_image is not None:
        dispatch.photo = first_image
    dispatch.completed_at = timezone.now()
    dispatch.status = DispatchOrder.Status.ACCEPTING
    dispatch.is_rejected = False
    dispatch.save()

    for upload, media_type, _size_limit in before_rows:
        DispatchMedia.objects.create(
            dispatch_order=dispatch,
            media_type=media_type,
            phase=DispatchMedia.Phase.BEFORE,
            file=upload,
            filename=upload.name[:255],
            uploaded_by=request.user,
        )
    for upload, media_type, _size_limit in after_rows:
        DispatchMedia.objects.create(
            dispatch_order=dispatch,
            media_type=media_type,
            phase=DispatchMedia.Phase.AFTER,
            file=upload,
            filename=upload.name[:255],
            uploaded_by=request.user,
        )

    total_count = len(before_rows) + len(after_rows)
    record_ticket_event(
        dispatch.repair_request,
        TicketEvent.EventType.COMPLETED,
        actor=request.user,
        detail=(
            f"工程队完工上报（共 {total_count} 个附件：维修前 {len(before_rows)}、维修后 {len(after_rows)}），"
            f"等待片区管理员验收"
        ),
        remark=dispatch.content or "",
    )

    notify_role(
        UserProfile.Role.REGION_ADMIN,
        f"派工单待验收：{dispatch.code}",
        f"工程队已完工上报，请管理员审核验收。",
        region=dispatch.repair_request.site.region,
        category="dispatch",
        business_code=dispatch.code,
    )
    log_operation(
        request.user, "派工", "完工上报",
        f"派工单 {dispatch.code} 完工并提交验收",
        request.META.get("REMOTE_ADDR"),
    )
    messages.success(request, "完工已上报，等待管理员审核。")
    return redirect("dispatch_detail", pk=pk)


@login_required
@require_POST
def dispatch_accept(request, pk):
    """工程队接单：待接单 -> 维修中，记录开工时间。"""
    dispatch = get_object_or_404(visible_dispatch_orders(request.user), pk=pk)
    if dispatch.status != DispatchOrder.Status.PENDING_ACCEPT or not _is_team_member(
        request.user, dispatch
    ):
        messages.error(request, "当前派工单不允许接单。")
        return redirect("dispatch_detail", pk=pk)

    dispatch.status = DispatchOrder.Status.IN_PROGRESS
    dispatch.started_at = timezone.now()
    dispatch.reject_reason = ""
    dispatch.save(update_fields=["status", "started_at", "reject_reason"])
    if dispatch.repair_request.equipment_id:
        dispatch.repair_request.equipment.refresh_status()
    record_ticket_event(
        dispatch.repair_request,
        TicketEvent.EventType.ACCEPTED,
        actor=request.user,
        detail=f"工程队「{dispatch.engineering_team.full_name}」已接单，开始维修",
    )
    notify_user(
        dispatch.repair_request.reporter,
        f"派工单 {dispatch.code} 已接单",
        f"工程队「{dispatch.engineering_team.full_name}」已接单并开始维修。",
        category="dispatch",
        business_code=dispatch.code,
    )
    notify_role(
        UserProfile.Role.SAFETY_REPAIR,
        f"派工单已接单：{dispatch.code}",
        f"工程队「{dispatch.engineering_team.full_name}」已接下派工单 {dispatch.code}。",
        category="dispatch",
        business_code=dispatch.code,
    )
    log_operation(
        request.user, "派工", "接单",
        f"派工单 {dispatch.code} 接单成功，开工时间 {dispatch.started_at:%Y-%m-%d %H:%M}",
        request.META.get("REMOTE_ADDR"),
    )
    messages.success(request, "已接单，请尽快开展维修。")
    return redirect("dispatch_detail", pk=pk)


@login_required
@require_POST
def dispatch_reject(request, pk):
    """工程队拒单：待接单 -> 已拒单，必须填写理由，单据退回待派工状态。"""
    dispatch = get_object_or_404(visible_dispatch_orders(request.user), pk=pk)
    if dispatch.status != DispatchOrder.Status.PENDING_ACCEPT or not _is_team_member(
        request.user, dispatch
    ):
        messages.error(request, "当前派工单不允许拒单。")
        return redirect("dispatch_detail", pk=pk)

    form = DispatchRejectForm(request.POST)
    if not form.is_valid():
        messages.error(request, "拒单必须填写理由：" + form.errors.as_text())
        return redirect("dispatch_detail", pk=pk)

    reason = form.cleaned_data["reject_reason"]
    dispatch.status = DispatchOrder.Status.REJECTED
    dispatch.reject_reason = reason
    dispatch.started_at = None
    dispatch.save(update_fields=["status", "reject_reason", "started_at"])
    repair = dispatch.repair_request
    repair.status = RepairRequest.Status.APPROVED
    repair.save(update_fields=["status"])
    if repair.equipment_id:
        repair.equipment.refresh_status()
    record_ticket_event(
        repair,
        TicketEvent.EventType.DISPATCH_REJECTED,
        actor=request.user,
        detail=f"工程队「{dispatch.engineering_team.full_name}」拒单，等待安数部改派",
        remark=reason,
    )
    notify_role(
        UserProfile.Role.SAFETY_REPAIR,
        f"派工单被拒单：{dispatch.code}",
        f"工程队「{dispatch.engineering_team.full_name}」拒绝了派工单 {dispatch.code}，"
        f"理由：{reason}。请改派其他工程队。",
        category="dispatch",
        business_code=dispatch.code,
    )
    log_operation(
        request.user, "派工", "拒单",
        f"派工单 {dispatch.code} 被拒单；理由：{reason}",
        request.META.get("REMOTE_ADDR"),
        result="failed",
    )
    messages.warning(request, "已拒单，安数部将改派其他工程队。")
    return redirect("dispatch_detail", pk=pk)


@login_required
@require_POST
def dispatch_reassign(request, pk):
    """安数部在拒单后改派：更换工程队并重新进入待接单。"""
    dispatch = get_object_or_404(visible_dispatch_orders(request.user), pk=pk)
    if not _is_safety(request.user):
        messages.error(request, "只有安数部维修岗可以改派。")
        return redirect("dispatch_detail", pk=pk)
    if dispatch.status != DispatchOrder.Status.REJECTED:
        messages.error(request, "只有已拒单的派工单可以改派。")
        return redirect("dispatch_detail", pk=pk)

    form = DispatchReassignForm(request.POST)
    if not form.is_valid():
        messages.error(request, "改派信息有误：" + form.errors.as_text())
        return redirect("dispatch_detail", pk=pk)

    team = form.cleaned_data["engineering_team"]
    old_team_name = dispatch.engineering_team.full_name
    dispatch.engineering_team = team
    dispatch.leader = form.cleaned_data.get("leader") or getattr(team, "bound_user", None)
    dispatch.status = DispatchOrder.Status.PENDING_ACCEPT
    dispatch.reassigned_times += 1
    dispatch.save(update_fields=["engineering_team", "leader", "status", "reassigned_times"])
    repair = dispatch.repair_request
    repair.status = RepairRequest.Status.ACCEPTING
    repair.save(update_fields=["status"])
    if repair.equipment_id:
        repair.equipment.refresh_status()
    record_ticket_event(
        repair,
        TicketEvent.EventType.REASSIGNED,
        actor=request.user,
        detail=f"安数部改派：{old_team_name} -> 「{team.full_name}」（第 {dispatch.reassigned_times} 次改派）",
        remark=dispatch.reject_reason,
    )
    recipients = [u for u in (team.bound_user, dispatch.leader) if u]
    for u in recipients:
        notify_user(
            u,
            f"改派任务待接单：{dispatch.code}",
            f"维修单 {repair.code}（{repair.site.name}）改派给你所在的工程队，请尽快接单。",
            category="dispatch",
            business_code=dispatch.code,
        )
    log_operation(
        request.user, "派工", "改派",
        f"派工单 {dispatch.code} 由 {old_team_name} 改派至 {team.full_name}",
        request.META.get("REMOTE_ADDR"),
    )
    messages.success(request, f"已改派给工程队「{team.full_name}」，等待接单。")
    return redirect("dispatch_detail", pk=pk)


@login_required
@require_POST
def dispatch_rate(request, pk):
    """报修人对完工服务进行三维度评价，每单仅一次。"""
    dispatch = get_object_or_404(visible_dispatch_orders(request.user), pk=pk)
    if dispatch.repair_request.reporter_id != request.user.id:
        messages.error(request, "只有报修人可以评价。")
        return redirect("dispatch_detail", pk=pk)
    if dispatch.status not in (
        DispatchOrder.Status.ACCEPTING,
        DispatchOrder.Status.ADMIN_REVIEW,
        DispatchOrder.Status.SAFETY_REVIEW,
        DispatchOrder.Status.CLOSED,
    ):
        messages.error(request, "工程队完工上报后才能评价。")
        return redirect("dispatch_detail", pk=pk)
    if hasattr(dispatch, "rating"):
        messages.error(request, "该派工单已评价，不能重复提交。")
        return redirect("dispatch_detail", pk=pk)

    form = AcceptanceRatingForm(request.POST)
    if not form.is_valid():
        messages.error(request, "评价信息有误：" + form.errors.as_text())
        return redirect("dispatch_detail", pk=pk)

    rating = AcceptanceRating.objects.create(
        dispatch_order=dispatch,
        response_speed=int(form.cleaned_data["response_speed"]),
        technical_level=int(form.cleaned_data["technical_level"]),
        service_attitude=int(form.cleaned_data["service_attitude"]),
        comment=form.cleaned_data.get("comment", ""),
        rater=request.user,
    )
    record_ticket_event(
        dispatch.repair_request,
        TicketEvent.EventType.RATED,
        actor=request.user,
        detail=f"报修人验收评价：综合 {rating.average_score} 星",
        remark=rating.comment,
    )
    log_operation(
        request.user, "派工", "验收评价",
        f"派工单 {dispatch.code} 综合评分 {rating.average_score} 星",
        request.META.get("REMOTE_ADDR"),
    )
    messages.success(request, f"评价已提交，综合 {rating.average_score} 星。")
    return redirect("dispatch_detail", pk=pk)


@login_required
@require_POST
def dispatch_review(request, pk):
    dispatch = get_object_or_404(visible_dispatch_orders(request.user), pk=pk)
    passed = request.POST.get("result") == "approved"
    note = request.POST.get("note", "").strip()

    if dispatch.status == DispatchOrder.Status.ACCEPTING and _is_region_admin(request.user):
        if passed:
            dispatch.status = DispatchOrder.Status.ADMIN_REVIEW
            dispatch.save(update_fields=["status"])
            notify_role(
                UserProfile.Role.SAFETY_REPAIR,
                f"派工单待安数部审核：{dispatch.code}",
                f"管理员已通过验收，请安数部审核闭环。",
                category="dispatch",
                business_code=dispatch.code,
            )
            messages.success(request, "管理员审核通过，流转安数部审核。")
            record_ticket_event(
                dispatch.repair_request,
                TicketEvent.EventType.REVIEW_PASSED,
                actor=request.user,
                detail="片区管理员验收通过，流转安数部审核闭环",
                remark=note,
            )
        else:
            dispatch.is_rejected = True
            dispatch.status = DispatchOrder.Status.IN_PROGRESS
            dispatch.completed_at = None
            dispatch.save(update_fields=["is_rejected", "status", "completed_at"])
            record_ticket_event(
                dispatch.repair_request,
                TicketEvent.EventType.REVIEW_REJECTED,
                actor=request.user,
                detail="片区管理员验收拒收，退回工程队返工",
                remark=note,
            )
            if dispatch.repair_request.equipment_id:
                dispatch.repair_request.equipment.refresh_status()
            team_users = [u for u in (dispatch.engineering_team.bound_user, dispatch.leader) if u]
            for u in team_users:
                notify_user(
                    u, f"派工单被拒收：{dispatch.code}",
                    f"管理员审核拒收，请返工。意见：{note or '无'}",
                    category="dispatch", business_code=dispatch.code,
                )
            messages.warning(request, "已拒收，单据退回工程队维修中。")
        log_operation(
            request.user, "派工", "管理员审核",
            f"派工单 {dispatch.code} {'通过' if passed else '拒收'}；意见：{note or '无'}",
            request.META.get("REMOTE_ADDR"),
            result="success" if passed else "failed",
        )
    else:
        messages.error(request, "当前状态或角色不允许管理员审核。")
    return redirect("dispatch_detail", pk=pk)


@login_required
@require_POST
def dispatch_final_review(request, pk):
    dispatch = get_object_or_404(visible_dispatch_orders(request.user), pk=pk)
    passed = request.POST.get("result") == "approved"
    note = request.POST.get("note", "").strip()

    if dispatch.status != DispatchOrder.Status.ADMIN_REVIEW or not _is_safety(request.user):
        messages.error(request, "当前状态或角色不允许安数部审核。")
        return redirect("dispatch_detail", pk=pk)

    if passed:
        dispatch.status = DispatchOrder.Status.CLOSED
        dispatch.save(update_fields=["status"])
        if dispatch.repair_request.equipment_id:
            dispatch.repair_request.equipment.refresh_status()
        record_ticket_event(
            dispatch.repair_request,
            TicketEvent.EventType.CLOSED,
            actor=request.user,
            detail="安数部审核通过，验收闭环，可发起结算",
            remark=note,
        )
        notify_user(
            dispatch.repair_request.reporter,
            f"维修单 {dispatch.repair_request.code} 验收闭环",
            "派工验收已全部通过，可以发起结算。",
            category="dispatch",
            business_code=dispatch.code,
        )
        messages.success(request, "安数部审核通过，验收闭环。")
    else:
        dispatch.is_rejected = True
        dispatch.status = DispatchOrder.Status.IN_PROGRESS
        dispatch.completed_at = None
        dispatch.save(update_fields=["is_rejected", "status", "completed_at"])
        if dispatch.repair_request.equipment_id:
            dispatch.repair_request.equipment.refresh_status()
        record_ticket_event(
            dispatch.repair_request,
            TicketEvent.EventType.REVIEW_REJECTED,
            actor=request.user,
            detail="安数部审核拒收，退回工程队返工",
            remark=note,
        )
        team_users = [u for u in (dispatch.engineering_team.bound_user, dispatch.leader) if u]
        for u in team_users:
            notify_user(
                u, f"派工单被安数部拒收：{dispatch.code}",
                f"安数部审核拒收，请返工。意见：{note or '无'}",
                category="dispatch", business_code=dispatch.code,
            )
        messages.warning(request, "已拒收，单据退回工程队维修中。")
    log_operation(
        request.user, "派工", "安数部审核",
        f"派工单 {dispatch.code} {'闭环通过' if passed else '拒收'}；意见：{note or '无'}",
        request.META.get("REMOTE_ADDR"),
        result="success" if passed else "failed",
    )
    return redirect("dispatch_detail", pk=pk)


@login_required
@require_POST
def settlement_create(request, pk):
    dispatch = get_object_or_404(visible_dispatch_orders(request.user), pk=pk)
    if not _is_safety(request.user):
        messages.error(request, "只有安数部维修岗可以创建结算单。")
        return redirect("dispatch_detail", pk=pk)
    if dispatch.status != DispatchOrder.Status.CLOSED:
        messages.error(request, "派工单验收闭环后才能创建结算单。")
        return redirect("dispatch_detail", pk=pk)
    if dispatch.settlement_orders.exists():
        messages.error(request, "该派工单已存在结算单。")
        return redirect("dispatch_detail", pk=pk)

    form = SettlementCreateForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.errors.as_text())
        return redirect("dispatch_detail", pk=pk)

    settlement = SettlementOrder.objects.create(
        dispatch_order=dispatch,
        amount=form.cleaned_data["amount"],
        creator=request.user,
    )
    notify_user(
        dispatch.repair_request.reporter,
        f"结算单已创建：{settlement.code}",
        f"结算金额 {settlement.amount} 元，待确认支付。",
        category="settlement",
        business_code=settlement.code,
    )
    log_operation(
        request.user, "结算", "创建结算单",
        f"结算单 {settlement.code}，金额 {settlement.amount}（派工单 {dispatch.code}）",
        request.META.get("REMOTE_ADDR"),
    )
    messages.success(request, f"结算单 {settlement.code} 已创建。")
    return redirect("dispatch_detail", pk=pk)


@login_required
@require_POST
def settlement_confirm(request, pk):
    settlement = get_object_or_404(
        SettlementOrder.objects.select_related(
            "dispatch_order__repair_request__site"
        ),
        pk=pk,
    )
    dispatch = settlement.dispatch_order
    if not _is_safety(request.user):
        messages.error(request, "只有安数部维修岗可以确认结算。")
        return redirect("dispatch_detail", pk=dispatch.pk)
    if settlement.is_settled:
        messages.error(request, "该结算单已确认。")
        return redirect("dispatch_detail", pk=dispatch.pk)

    settlement.is_settled = True
    settlement.save(update_fields=["is_settled"])
    repair = dispatch.repair_request
    repair.status = RepairRequest.Status.SETTLED
    repair.save(update_fields=["status"])
    record_ticket_event(
        repair,
        TicketEvent.EventType.SETTLED,
        actor=request.user,
        detail=f"结算单 {settlement.code}（{settlement.amount} 元）已支付确认，流程闭环",
    )
    notify_user(
        repair.reporter,
        f"维修单 {repair.code} 已结算",
        f"结算单 {settlement.code}（{settlement.amount} 元）已确认，流程闭环。",
        category="settlement",
        business_code=settlement.code,
    )
    log_operation(
        request.user, "结算", "确认结算",
        f"结算单 {settlement.code} 已支付确认，维修单 {repair.code} 流程闭环",
        request.META.get("REMOTE_ADDR"),
    )
    messages.success(request, "已确认结算，维修单流程闭环。")
    return redirect("dispatch_detail", pk=dispatch.pk)


# ===================== 备件库存 =====================

@login_required
def sparepart_list(request):
    parts = SparePart.objects.select_related("material")
    keyword = request.GET.get("q", "").strip()
    if keyword:
        parts = parts.filter(
            Q(code__icontains=keyword)
            | Q(name__icontains=keyword)
            | Q(spec__icontains=keyword)
        )
    show_low = request.GET.get("low") == "1"
    if show_low:
        parts = parts.filter(stock_quantity__lte=F("safety_stock"))
    status = request.GET.get("status", "")
    if status == "active":
        parts = parts.filter(is_active=True)
    elif status == "inactive":
        parts = parts.filter(is_active=False)

    low_count = SparePart.objects.filter(
        is_active=True, stock_quantity__lte=F("safety_stock")
    ).count()
    return render(
        request,
        "approvals/sparepart_list.html",
        {
            "parts": parts,
            "keyword": keyword,
            "show_low": show_low,
            "status": status,
            "low_count": low_count,
            "can_manage": _is_safety(request.user),
        },
    )


@login_required
def sparepart_detail(request, pk):
    part = get_object_or_404(SparePart.objects.select_related("material"), pk=pk)
    movements = part.movements.select_related("operator", "dispatch_order")[:50]
    return render(
        request,
        "approvals/sparepart_detail.html",
        {
            "part": part,
            "movements": movements,
            "in_form": StockInForm(),
            "out_form": StockOutForm(),
            "can_manage": _is_safety(request.user),
        },
    )


@login_required
def sparepart_add(request):
    if not _is_safety(request.user):
        messages.error(request, "只有安数部维修岗可以维护备件档案。")
        return redirect("sparepart_list")
    if request.method == "POST":
        form = SparePartForm(request.POST)
        if form.is_valid():
            part = form.save()
            log_operation(
                request.user, "库存", "新增备件",
                f"备件 {part.code} {part.name}",
                request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, f"备件 {part.code} 已创建。")
            return redirect("sparepart_detail", pk=part.pk)
    else:
        form = SparePartForm()
    return render(request, "approvals/sparepart_form.html", {"form": form, "title": "新增备件"})


@login_required
def sparepart_edit(request, pk):
    if not _is_safety(request.user):
        messages.error(request, "只有安数部维修岗可以维护备件档案。")
        return redirect("sparepart_list")
    part = get_object_or_404(SparePart, pk=pk)
    if request.method == "POST":
        form = SparePartForm(request.POST, instance=part)
        if form.is_valid():
            form.save()
            log_operation(
                request.user, "库存", "编辑备件",
                f"备件 {part.code} {part.name}",
                request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, f"备件 {part.code} 已更新。")
            return redirect("sparepart_detail", pk=part.pk)
    else:
        form = SparePartForm(instance=part)
    return render(
        request,
        "approvals/sparepart_form.html",
        {"form": form, "part": part, "title": f"编辑备件 {part.code}"},
    )


def _handle_stock_post(request, pk, movement_type):
    """入库/出库登记的共用处理（库存变更只走 apply_stock_movement）。"""
    if not _is_safety(request.user):
        messages.error(request, "只有安数部维修岗可以登记出入库。")
        return redirect("sparepart_list")
    part = get_object_or_404(SparePart, pk=pk)
    form_cls = StockInForm if movement_type == StockMovement.MovementType.IN else StockOutForm
    form = form_cls(request.POST)
    if not form.is_valid():
        messages.error(request, "出入库信息有误：" + form.errors.as_text())
        return redirect("sparepart_detail", pk=pk)

    dispatch_order = form.cleaned_data.get("dispatch_order")
    try:
        movement = apply_stock_movement(
            part,
            movement_type,
            form.cleaned_data["quantity"],
            operator=request.user,
            dispatch_order=dispatch_order,
            remark=form.cleaned_data.get("remark", ""),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("sparepart_detail", pk=pk)

    label = "入库" if movement_type == StockMovement.MovementType.IN else "出库"
    log_operation(
        request.user, "库存", f"备件{label}",
        f"{label} {part.code} {part.name} × {movement.quantity}，结存 {movement.balance_after}"
        + (f"（派工单 {dispatch_order.code}）" if dispatch_order else ""),
        request.META.get("REMOTE_ADDR"),
    )
    if movement_type == StockMovement.MovementType.OUT and dispatch_order is not None:
        record_ticket_event(
            dispatch_order.repair_request,
            TicketEvent.EventType.STOCK_USED,
            actor=request.user,
            detail=(
                f"备件领用：{part.code} {part.name} × {movement.quantity}"
                f"{part.unit or ''}"
            ),
            remark=movement.remark,
        )
    # 出库后库存不高于安全库存 -> 预警通知安数部
    if movement_type == StockMovement.MovementType.OUT:
        part.refresh_from_db(fields=["stock_quantity"])
        if part.is_low_stock:
            notify_role(
                UserProfile.Role.SAFETY_REPAIR,
                f"备件低库存预警：{part.name}",
                f"备件 {part.code} {part.name} 当前库存 {part.stock_quantity}{part.unit or ''}，"
                f"已不高于安全库存 {part.safety_stock}，请及时补货。",
                category="stock",
                business_code=part.code,
            )
    messages.success(request, f"{label}成功，当前结存 {movement.balance_after}。")
    return redirect("sparepart_detail", pk=pk)


@login_required
@require_POST
def sparepart_stock_in(request, pk):
    return _handle_stock_post(request, pk, StockMovement.MovementType.IN)


@login_required
@require_POST
def sparepart_stock_out(request, pk):
    return _handle_stock_post(request, pk, StockMovement.MovementType.OUT)
