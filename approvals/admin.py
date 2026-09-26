from django.contrib import admin

from .models import (
    AcceptanceRating,
    ApprovalFlowConfig,
    ApprovalRecord,
    CommonPhrase,
    Dictionary,
    DispatchMedia,
    DispatchOrder,
    EngineeringTeam,
    Equipment,
    Material,
    Notification,
    OperationLog,
    Region,
    RepairMaterialItem,
    RepairMedia,
    RepairRequest,
    SettlementOrder,
    SparePart,
    StockMovement,
    TicketEvent,
    Site,
    UserProfile,
)


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "leader")
    search_fields = ("code", "name", "leader__username")


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "address", "region", "manager")
    list_filter = ("region",)
    search_fields = ("code", "name", "address", "manager__username")


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "site",
        "name",
        "category",
        "model_spec",
        "serial_number",
        "location",
        "status",
        "warranty_expiry",
        "inspection_expiry",
    )
    list_filter = ("category", "status", "site__region", "site")
    search_fields = ("code", "name", "model_spec", "serial_number", "location", "site__name")
    date_hierarchy = "commission_date"
    readonly_fields = ("code", "created_at", "updated_at")


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "material_type",
        "name",
        "spec",
        "unit",
        "standard_quantity",
        "max_unit_price",
    )
    list_filter = ("material_type",)
    search_fields = ("name", "spec", "material_type")


@admin.register(SparePart)
class SparePartAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "spec",
        "unit",
        "stock_quantity",
        "safety_stock",
        "reference_price",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active",)
    search_fields = ("code", "name", "spec")
    readonly_fields = ("code", "stock_quantity", "created_at", "updated_at")
    fieldsets = (
        ("档案信息", {
            "fields": ("code", "name", "spec", "unit", "material", "reference_price", "is_active", "remark"),
        }),
        ("库存（由出入库流水维护，请勿直接修改）", {
            "fields": ("stock_quantity", "safety_stock"),
        }),
        ("时间", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "spare_part",
        "movement_type",
        "quantity",
        "balance_after",
        "dispatch_order",
        "operator",
        "created_at",
    )
    list_filter = ("movement_type", "created_at")
    search_fields = ("spare_part__code", "spare_part__name", "remark", "dispatch_order__code")
    date_hierarchy = "created_at"
    readonly_fields = (
        "spare_part",
        "movement_type",
        "quantity",
        "balance_after",
        "dispatch_order",
        "operator",
        "remark",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class RepairMaterialItemInline(admin.TabularInline):
    model = RepairMaterialItem
    extra = 1
    fields = ("material", "custom_name", "quantity", "unit_price", "subtotal")
    readonly_fields = ("subtotal",)


@admin.register(RepairRequest)
class RepairRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "code",
        "site",
        "reporter",
        "repair_vendor",
        "repair_type",
        "urgency",
        "equipment",
        "equipment_name",
        "short_description",
        "budget_amount",
        "status",
        "parent",
        "created_at",
    )
    list_filter = (
        "status",
        "repair_type",
        "urgency",
        "site",
        "site__region",
        "created_at",
    )
    search_fields = (
        "code",
        "description",
        "equipment_name",
        "repair_vendor",
        "contact_phone",
        "equipment__code",
        "equipment__name",
        "site__name",
        "site__code",
        "reporter__username",
        "parent__code",
    )
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "code")
    inlines = (RepairMaterialItemInline,)

    @admin.display(description="问题描述")
    def short_description(self, obj):
        text = obj.description
        return text if len(text) <= 30 else f"{text[:30]}…"


@admin.register(ApprovalRecord)
class ApprovalRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "repair_request", "node", "step", "approver", "result", "approved_at")
    list_filter = ("result", "node", "step", "approver")
    search_fields = ("repair_request__code", "approver__username", "comment")
    date_hierarchy = "approved_at"


@admin.register(EngineeringTeam)
class EngineeringTeamAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "code",
        "full_name",
        "credit_code",
        "legal_person",
        "qualification_level",
        "bound_user",
    )
    list_filter = ("qualification_level",)
    search_fields = (
        "code",
        "full_name",
        "credit_code",
        "legal_person",
        "bank_account",
        "bound_user__username",
    )


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "role", "region", "site")
    list_filter = ("role", "region", "site")
    search_fields = ("user__username", "user__first_name", "user__last_name")


class DispatchMediaInline(admin.TabularInline):
    model = DispatchMedia
    extra = 0
    fields = ("media_type", "phase", "file", "filename", "uploaded_by", "created_at")
    readonly_fields = ("created_at",)


@admin.register(DispatchOrder)
class DispatchOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "code",
        "repair_request",
        "engineering_team",
        "leader",
        "status",
        "reassigned_times",
        "is_rejected",
        "started_at",
        "completed_at",
        "created_at",
    )
    list_filter = ("status", "is_rejected", "engineering_team", "created_at")
    search_fields = (
        "code",
        "repair_request__code",
        "engineering_team__full_name",
        "engineering_team__code",
        "leader__username",
    )
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "code")
    inlines = [DispatchMediaInline]
    fieldsets = (
        ("派工信息", {
            "fields": (
                "code", "repair_request", "engineering_team", "leader",
                "status", "reassigned_times", "reject_reason", "is_rejected",
            )
        }),
        ("维修过程", {
            "fields": (
                "content", "actual_fault", "repair_plan", "actual_measures",
                "work_hours", "started_at", "completed_at",
            )
        }),
        ("其他", {"fields": ("photo", "created_at")}),
    )


@admin.register(DispatchMedia)
class DispatchMediaAdmin(admin.ModelAdmin):
    list_display = (
        "id", "dispatch_order", "media_type", "phase", "filename", "uploaded_by", "created_at",
    )
    list_filter = ("media_type", "phase", "created_at")
    search_fields = ("filename", "dispatch_order__code")
    readonly_fields = ("created_at",)


@admin.register(AcceptanceRating)
class AcceptanceRatingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "dispatch_order",
        "response_speed",
        "technical_level",
        "service_attitude",
        "rater",
        "created_at",
    )
    list_filter = ("response_speed", "technical_level", "service_attitude", "created_at")
    search_fields = ("dispatch_order__code", "comment", "rater__username")
    readonly_fields = ("created_at",)


@admin.register(RepairMedia)
class RepairMediaAdmin(admin.ModelAdmin):
    list_display = ("id", "repair_request", "media_type", "filename", "uploaded_by", "created_at")
    list_filter = ("media_type", "created_at")
    search_fields = ("filename", "repair_request__code")
    readonly_fields = ("created_at",)


@admin.register(TicketEvent)
class TicketEventAdmin(admin.ModelAdmin):
    list_display = ("id", "repair_request", "event_type", "detail", "actor", "created_at")
    list_filter = ("event_type", "created_at")
    search_fields = ("repair_request__code", "detail", "remark", "actor__username")
    date_hierarchy = "created_at"
    readonly_fields = (
        "repair_request",
        "event_type",
        "actor",
        "detail",
        "remark",
        "created_at",
    )

    # 历程事件只增不改：后台仅允许查看
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SettlementOrder)
class SettlementOrderAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "dispatch_order", "amount", "creator", "is_settled", "created_at")
    list_filter = ("is_settled", "created_at")
    search_fields = ("code", "dispatch_order__code", "creator__username")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "code")


@admin.register(ApprovalFlowConfig)
class ApprovalFlowConfigAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "flow_type",
        "node",
        "timeout_days",
        "warn_before_days",
        "warn_roles",
        "is_active",
    )
    list_filter = ("flow_type", "node", "is_active")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "category",
        "recipient",
        "business_code",
        "is_read",
        "created_at",
    )
    list_filter = ("category", "is_read", "created_at")
    search_fields = ("title", "content", "business_code", "recipient__username")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "read_at")


@admin.register(OperationLog)
class OperationLogAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "module", "action", "ip", "result", "created_at")
    list_filter = ("result", "module", "created_at")
    search_fields = ("module", "action", "content", "ip", "user__username")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Dictionary)
class DictionaryAdmin(admin.ModelAdmin):
    list_display = ("id", "category", "code", "label", "sort_order", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("category", "code", "label")


@admin.register(CommonPhrase)
class CommonPhraseAdmin(admin.ModelAdmin):
    list_display = ("id", "node", "short_content", "sort_order", "is_active", "creator")
    list_filter = ("node", "is_active")
    search_fields = ("content",)

    @admin.display(description="常用语内容")
    def short_content(self, obj):
        return obj.content if len(obj.content) <= 25 else f"{obj.content[:25]}…"
