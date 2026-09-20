from django import forms
from django.forms import modelformset_factory
from django.contrib.auth import get_user_model

from .models import (
    DispatchOrder,
    EngineeringTeam,
    Equipment,
    Material,
    RepairMaterialItem,
    RepairRequest,
    Site,
    SparePart,
    UserProfile,
)


def visible_sites(user):
    """按用户角色返回其可见油站查询集。"""
    qs = Site.objects.select_related("region")
    if user.is_superuser:
        return qs
    profile = getattr(user, "profile", None)
    if profile is None:
        return Site.objects.none()
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
            return qs.filter(region_id=profile.region_id)
        return Site.objects.none()
    if role == UserProfile.Role.SITE_MANAGER:
        if profile.site_id:
            return qs.filter(pk=profile.site_id)
        return Site.objects.none()
    return Site.objects.none()


def visible_equipments(user):
    """按用户可见油站范围返回设备查询集。"""
    return (
        Equipment.objects.filter(site__in=visible_sites(user))
        .select_related("site", "site__region")
    )


def can_manage_equipment(user, equipment=None):
    """是否可新增/编辑设备：总部各级 + 片区管理员/经理（本片区）+ 站长（本站）。"""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    if profile is None:
        return False
    if profile.role in (
        UserProfile.Role.SAFETY_REPAIR,
        UserProfile.Role.DEPUTY_GM,
        UserProfile.Role.GM,
        UserProfile.Role.CHAIRMAN,
    ):
        return True
    if profile.role in (UserProfile.Role.REGION_ADMIN, UserProfile.Role.REGION_MANAGER):
        if equipment is None:
            return bool(profile.region_id)
        return equipment.site.region_id == profile.region_id
    if profile.role == UserProfile.Role.SITE_MANAGER:
        if equipment is None:
            return bool(profile.site_id)
        return equipment.site_id == profile.site_id
    return False


class EquipmentForm(forms.ModelForm):
    class Meta:
        model = Equipment
        fields = (
            "site",
            "name",
            "category",
            "model_spec",
            "serial_number",
            "location",
            "commission_date",
            "warranty_expiry",
            "inspection_expiry",
            "next_maintenance_date",
            "supplier",
            "status",
            "remark",
        )
        widgets = {
            "commission_date": forms.DateInput(attrs={"type": "date"}),
            "warranty_expiry": forms.DateInput(attrs={"type": "date"}),
            "inspection_expiry": forms.DateInput(attrs={"type": "date"}),
            "next_maintenance_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["site"].queryset = visible_sites(user)
            profile = getattr(user, "profile", None)
            if profile and profile.site_id and not self.instance.pk:
                if self.fields["site"].queryset.filter(pk=profile.site_id).exists():
                    self.fields["site"].initial = profile.site_id


def visible_repair_requests(user):
    """按用户角色返回其可见维修单查询集。"""
    qs = RepairRequest.objects.select_related("site", "reporter", "parent")
    if user.is_superuser:
        return qs
    profile = getattr(user, "profile", None)
    if profile is None:
        return qs.filter(reporter=user)
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
            return qs.filter(site__region_id=profile.region_id)
        return qs.none()
    if role == UserProfile.Role.SITE_MANAGER:
        if profile.site_id:
            return qs.filter(site_id=profile.site_id)
        return qs.filter(reporter=user)
    return qs.filter(reporter=user)


class RepairRequestForm(forms.ModelForm):
    class Meta:
        model = RepairRequest
        fields = (
            "site",
            "parent",
            "equipment",
            "repair_type",
            "urgency",
            "equipment_name",
            "description",
            "budget_amount",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "equipment_name": forms.TextInput(attrs={"placeholder": "如：3号加油机"}),
            "budget_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if user is not None:
            sites = visible_sites(user)
            self.fields["site"].queryset = sites
            profile = getattr(user, "profile", None)
            if profile and profile.site_id and sites.filter(pk=profile.site_id).exists():
                self.fields["site"].initial = profile.site_id
            # 设备档案：仅限本人可见油站；扫码报修预填由视图设置 initial
            self.fields["equipment"].queryset = (
                Equipment.objects.filter(site__in=sites)
                .exclude(status=Equipment.Status.DEACTIVATED)
                .select_related("site")
                .order_by("site__code", "code")
            )
            # 追加来源：本用户可见的、已进入审批/执行阶段的维修单
            self.fields["parent"].queryset = (
                visible_repair_requests(user)
                .exclude(status=RepairRequest.Status.DRAFT)
                .order_by("-created_at")
            )
        self.fields["parent"].required = False
        self.fields["parent"].label = "追加来源单据"
        self.fields["parent"].empty_label = "无（独立单据）"
        self.fields["equipment"].required = False
        self.fields["equipment"].label = "关联设备档案"
        self.fields["equipment"].empty_label = "不关联（手动填写设备名称）"


class RepairMaterialItemForm(forms.ModelForm):
    class Meta:
        model = RepairMaterialItem
        fields = ("material", "custom_name", "quantity", "unit_price")
        widgets = {
            "quantity": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "unit_price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "custom_name": forms.TextInput(attrs={"placeholder": "选择非标准材料时填写名称"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["material"].queryset = Material.objects.filter(
            material_type__in=["internal", "external"]
        )
        self.fields["material"].required = False
        self.fields["custom_name"].required = False

    def clean(self):
        cleaned = super().clean()
        material = cleaned.get("material")
        custom_name = (cleaned.get("custom_name") or "").strip()
        quantity = cleaned.get("quantity")
        unit_price = cleaned.get("unit_price")
        # 整行为空则跳过
        if not material and not custom_name and not quantity and not unit_price:
            cleaned["_empty"] = True
            return cleaned
        if not material and not custom_name:
            raise forms.ValidationError("请选择标准材料，或填写自定义材料名称")
        if material and custom_name:
            raise forms.ValidationError("标准材料与自定义名称二选一")
        if quantity is None or quantity <= 0:
            self.add_error("quantity", "数量必须大于 0")
        if unit_price is None or unit_price < 0:
            self.add_error("unit_price", "单价不能为负")
        if not custom_name and material:
            cleaned["custom_name"] = ""
        return cleaned


RepairMaterialItemFormSet = modelformset_factory(
    RepairMaterialItem,
    form=RepairMaterialItemForm,
    extra=4,
    can_delete=True,
)


class DispatchCreateForm(forms.Form):
    """安数部派工：为已审批完成、尚未派工的维修单创建派工单。"""

    repair_request = forms.ModelChoiceField(
        label="维修单",
        queryset=RepairRequest.objects.filter(
            status=RepairRequest.Status.APPROVED
        ).order_by("-created_at"),
    )
    engineering_team = forms.ModelChoiceField(
        label="工程队",
        queryset=EngineeringTeam.objects.all(),
    )
    leader = forms.ModelChoiceField(
        label="负责人",
        queryset=get_user_model().objects.filter(is_active=True),
        required=False,
    )
    content = forms.CharField(label="维修内容", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # 已存在派工单的维修单排除掉（一对一）
        self.fields["repair_request"].queryset = RepairRequest.objects.filter(
            status=RepairRequest.Status.APPROVED
        ).exclude(dispatch_order__isnull=False)


class DispatchCompleteForm(forms.Form):
    """工程队完工上报（多照片/视频文件由视图直接从 request.FILES 处理）。"""

    content = forms.CharField(label="维修内容", widget=forms.Textarea(attrs={"rows": 3}), required=False)
    actual_fault = forms.CharField(
        label="实际故障情况",
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )
    repair_plan = forms.CharField(
        label="维修方案",
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )
    actual_measures = forms.CharField(
        label="实际维修措施",
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )
    work_hours = forms.DecimalField(
        label="维修工时（小时）",
        max_digits=6,
        decimal_places=2,
        min_value=0,
        required=False,
        widget=forms.NumberInput(attrs={"step": "0.25", "min": "0"}),
    )


class DispatchRejectForm(forms.Form):
    """工程队拒单：必须说明理由。"""

    reject_reason = forms.CharField(
        label="拒单理由",
        max_length=255,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class DispatchReassignForm(forms.Form):
    """安数部在工程队拒单后改派其他工程队。"""

    engineering_team = forms.ModelChoiceField(
        label="改派工程队",
        queryset=EngineeringTeam.objects.all(),
    )
    leader = forms.ModelChoiceField(
        label="负责人（可选）",
        queryset=get_user_model().objects.filter(is_active=True),
        required=False,
    )


class AcceptanceRatingForm(forms.Form):
    """报修人对完工服务三维度打分。"""

    SCORE_CHOICES = [(i, f"{i} 星") for i in range(1, 6)]
    response_speed = forms.ChoiceField(label="响应速度", choices=SCORE_CHOICES, initial=5)
    technical_level = forms.ChoiceField(label="技术水平", choices=SCORE_CHOICES, initial=5)
    service_attitude = forms.ChoiceField(label="服务态度", choices=SCORE_CHOICES, initial=5)
    comment = forms.CharField(
        label="评价内容",
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )


class SettlementCreateForm(forms.Form):
    amount = forms.DecimalField(
        label="结算金额（元）",
        max_digits=14,
        decimal_places=2,
        min_value=0,
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )


class SparePartForm(forms.ModelForm):
    """备件档案维护（编码自动生成、库存由出入库流水维护，均不在此编辑）。"""

    class Meta:
        model = SparePart
        fields = (
            "name",
            "spec",
            "unit",
            "material",
            "safety_stock",
            "reference_price",
            "is_active",
            "remark",
        )
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "如：交流接触器"}),
            "spec": forms.TextInput(attrs={"placeholder": "如：CJX2-2510 220V"}),
            "safety_stock": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "reference_price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "remark": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["material"].required = False
        self.fields["material"].queryset = Material.objects.order_by(
            "material_type", "name"
        )


class StockInForm(forms.Form):
    """入库登记（补货/期初入库）。"""

    quantity = forms.DecimalField(
        label="入库数量",
        max_digits=12,
        decimal_places=2,
        min_value=0.01,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
    )
    remark = forms.CharField(
        label="备注",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "如：采购入库 / 期初盘点"}),
    )


class StockOutForm(forms.Form):
    """出库登记（领用，可关联派工单）。"""

    quantity = forms.DecimalField(
        label="出库数量",
        max_digits=12,
        decimal_places=2,
        min_value=0.01,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
    )
    dispatch_order = forms.ModelChoiceField(
        label="关联派工单（可选）",
        queryset=DispatchOrder.objects.exclude(
            status=DispatchOrder.Status.CLOSED
        ).order_by("-created_at"),
        required=False,
    )
    remark = forms.CharField(
        label="备注",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "如：维修领用"}),
    )
