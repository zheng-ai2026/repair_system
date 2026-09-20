from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


class Region(models.Model):
    """片区"""

    code = models.CharField("片区编号", max_length=50, unique=True)
    name = models.CharField("片区名称", max_length=100)
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="片区领导",
        related_name="led_regions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "片区"
        verbose_name_plural = "片区"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class Site(models.Model):
    """油站"""

    code = models.CharField("油站编号", max_length=50, unique=True)
    name = models.CharField("油站名称", max_length=100)
    address = models.CharField("油站地址", max_length=255, blank=True)
    region = models.ForeignKey(
        Region,
        verbose_name="所属片区",
        related_name="sites",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="站长",
        related_name="managed_sites",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "油站"
        verbose_name_plural = "油站"
        ordering = ["code"]

    def __str__(self):
        return f"[{self.code}] {self.name}" if self.code else self.name


class Equipment(models.Model):
    """设备档案：一物一码，关联油站，汇聚维修履历。"""

    class Category(models.TextChoices):
        FUEL_DISPENSER = "fuel_dispenser", "加油机"
        LEVEL_GAUGE = "level_gauge", "液位仪"
        SECURITY = "security", "安防设备"
        ELECTRICAL = "electrical", "配电设备"
        FIRE_FIGHTING = "fire_fighting", "消防设备"
        OTHER = "other", "其他设备"

    class Status(models.TextChoices):
        NORMAL = "normal", "正常"
        FAULT = "fault", "故障"
        IN_REPAIR = "in_repair", "维修中"
        DEACTIVATED = "deactivated", "停用"

    # 关联单据进行中时视为"故障待处理"的维修单状态
    # 说明：accepting（维修/验收中）由派工单状态判定，闭环后即便未结算也视为修复
    ACTIVE_REPAIR_STATUSES = (
        "pending_region_admin",
        "pending_region_manager",
        "pending_safety_repair",
        "pending_deputy_gm",
        "pending_gm",
        "pending_chairman",
        "approved",
    )
    ACTIVE_DISPATCH_STATUSES = (
        "pending_accept",
        "in_progress",
        "accepting",
        "admin_review",
        "safety_review",
    )

    code = models.CharField("设备编号", max_length=50, unique=True, blank=True)
    site = models.ForeignKey(
        Site,
        verbose_name="所属油站",
        related_name="equipments",
        on_delete=models.PROTECT,
    )
    name = models.CharField("设备名称", max_length=200)
    category = models.CharField(
        "设备类别",
        max_length=20,
        choices=Category.choices,
        default=Category.OTHER,
    )
    model_spec = models.CharField("型号规格", max_length=100, blank=True)
    serial_number = models.CharField("出厂序列号", max_length=100, blank=True)
    location = models.CharField("安装位置", max_length=200, blank=True)
    commission_date = models.DateField("投运日期", null=True, blank=True)
    warranty_expiry = models.DateField("保修到期", null=True, blank=True)
    inspection_expiry = models.DateField("检验到期", null=True, blank=True)
    next_maintenance_date = models.DateField("下次保养日期", null=True, blank=True)
    supplier = models.CharField("供应商", max_length=200, blank=True)
    status = models.CharField(
        "设备状态",
        max_length=20,
        choices=Status.choices,
        default=Status.NORMAL,
    )
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("建档时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "设备档案"
        verbose_name_plural = "设备档案"
        ordering = ["site__code", "code"]
        indexes = [
            models.Index(fields=["category"], name="equip_category_idx"),
            models.Index(fields=["status"], name="equip_status_idx"),
        ]

    @classmethod
    def generate_code(cls, at=None):
        """生成编号：EQ + 年月 + 4 位全局序号。"""
        at = at or timezone.now()
        prefix = f"EQ{at.strftime('%Y%m')}"
        last = (
            cls.objects.select_for_update()
            .filter(code__startswith=prefix)
            .order_by("-code")
            .first()
        )
        sequence = 1
        if last is not None:
            try:
                sequence = int(last.code[-4:]) + 1
            except ValueError:
                sequence = cls.objects.filter(code__startswith=prefix).count() + 1
        return f"{prefix}{sequence:04d}"

    def save(self, *args, **kwargs):
        if not self.code:
            with transaction.atomic():
                self.code = self.generate_code()
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def _days_left(self, date_value):
        if not date_value:
            return None
        return (date_value - timezone.localdate()).days

    @property
    def warranty_days_left(self):
        return self._days_left(self.warranty_expiry)

    @property
    def inspection_days_left(self):
        return self._days_left(self.inspection_expiry)

    @property
    def maintenance_days_left(self):
        return self._days_left(self.next_maintenance_date)

    def expiry_alerts(self, warn_days=30):
        """返回临近/已到期的提醒项（名称、日期、剩余天数）。"""
        items = [
            ("保修到期", self.warranty_expiry, self.warranty_days_left),
            ("检验到期", self.inspection_expiry, self.inspection_days_left),
            ("保养到期", self.next_maintenance_date, self.maintenance_days_left),
        ]
        return [
            {
                "label": label,
                "date": d,
                "days_left": days,
                "overdue_days": -days if days < 0 else 0,
            }
            for label, d, days in items
            if d is not None and days <= warn_days
        ]

    def refresh_status(self, save=True):
        """按在途单据重算设备状态（手动停用的设备不自动恢复）。

        有在途派工单 -> 维修中；否则有在途维修单 -> 故障；否则 -> 正常。
        """
        if self.status == self.Status.DEACTIVATED:
            return self.status
        if self.repair_requests.filter(
            dispatch_order__status__in=self.ACTIVE_DISPATCH_STATUSES
        ).exists():
            new_status = self.Status.IN_REPAIR
        elif self.repair_requests.filter(
            status__in=self.ACTIVE_REPAIR_STATUSES
        ).exists():
            new_status = self.Status.FAULT
        else:
            new_status = self.Status.NORMAL
        if new_status != self.status:
            self.status = new_status
            if save:
                self.save(update_fields=["status", "updated_at"])
        return new_status

    def __str__(self):
        return f"{self.code} {self.name}（{self.site.name}）"


class Material(models.Model):
    """标准化材料（总部管理员维护）"""

    class MaterialSource(models.TextChoices):
        INTERNAL = "internal", "内部"
        EXTERNAL = "external", "外部"

    material_type = models.CharField(
        "材料类型",
        max_length=10,
        choices=MaterialSource.choices,
        default=MaterialSource.INTERNAL,
    )
    name = models.CharField("名称", max_length=200)
    spec = models.CharField("规格", max_length=100, blank=True)
    unit = models.CharField("单位", max_length=20, blank=True)
    standard_quantity = models.DecimalField(
        "定额数量", max_digits=12, decimal_places=2, default=1
    )
    max_unit_price = models.DecimalField("单价上限", max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = "标准化材料"
        verbose_name_plural = "标准化材料"
        ordering = ["material_type", "name"]

    def __str__(self):
        return f"{self.name}（{self.spec}）" if self.spec else self.name


class SparePart(models.Model):
    """备件档案（全局单仓库库存）。

    stock_quantity 是库存快照，只允许通过 services.apply_stock_movement
    随出入库流水一起更新，其它代码不得直接改写。
    """

    code = models.CharField("备件编码", max_length=50, unique=True, blank=True)
    name = models.CharField("备件名称", max_length=200)
    spec = models.CharField("规格型号", max_length=100, blank=True)
    unit = models.CharField("单位", max_length=20, blank=True, default="件")
    material = models.ForeignKey(
        Material,
        verbose_name="关联标准化材料",
        related_name="spare_parts",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    stock_quantity = models.DecimalField(
        "当前库存", max_digits=12, decimal_places=2, default=0
    )
    safety_stock = models.DecimalField(
        "安全库存", max_digits=12, decimal_places=2, default=0
    )
    reference_price = models.DecimalField(
        "参考单价", max_digits=12, decimal_places=2, null=True, blank=True
    )
    is_active = models.BooleanField("启用", default=True)
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("建档时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "备件档案"
        verbose_name_plural = "备件档案"
        ordering = ["code"]
        indexes = [
            models.Index(fields=["is_active"], name="spare_active_idx"),
        ]

    @classmethod
    def generate_code(cls, at=None):
        """生成编码：SP + 年月 + 4 位全局序号。"""
        at = at or timezone.now()
        prefix = f"SP{at.strftime('%Y%m')}"
        last = (
            cls.objects.select_for_update()
            .filter(code__startswith=prefix)
            .order_by("-code")
            .first()
        )
        sequence = 1
        if last is not None:
            try:
                sequence = int(last.code[-4:]) + 1
            except ValueError:
                sequence = cls.objects.filter(code__startswith=prefix).count() + 1
        return f"{prefix}{sequence:04d}"

    def save(self, *args, **kwargs):
        if not self.code:
            with transaction.atomic():
                self.code = self.generate_code()
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    @property
    def is_low_stock(self):
        return self.stock_quantity <= self.safety_stock

    def __str__(self):
        return f"{self.code} {self.name}"


class StockMovement(models.Model):
    """备件出入库流水（只追加、不修改，库存快照可由流水聚合推导）。"""

    class MovementType(models.TextChoices):
        IN = "in", "入库"
        OUT = "out", "出库"

    spare_part = models.ForeignKey(
        SparePart,
        verbose_name="备件",
        related_name="movements",
        on_delete=models.PROTECT,
    )
    movement_type = models.CharField("类型", max_length=10, choices=MovementType.choices)
    quantity = models.DecimalField("数量", max_digits=12, decimal_places=2)
    balance_after = models.DecimalField("结存数量", max_digits=12, decimal_places=2)
    dispatch_order = models.ForeignKey(
        "DispatchOrder",
        verbose_name="关联派工单",
        related_name="stock_movements",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="经办人",
        related_name="stock_movements",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    remark = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField("登记时间", auto_now_add=True)

    class Meta:
        verbose_name = "出入库流水"
        verbose_name_plural = "出入库流水"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        sign = "+" if self.movement_type == self.MovementType.IN else "-"
        return f"{self.spare_part.code} {sign}{self.quantity}（结存 {self.balance_after}）"


class RepairRequest(models.Model):
    """维修单"""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_REGION_ADMIN = "pending_region_admin", "待片区管理员汇总"
        PENDING_REGION_MANAGER = "pending_region_manager", "待片区经理审批"
        PENDING_SAFETY_REPAIR = "pending_safety_repair", "待安数部维修岗审批"
        PENDING_DEPUTY_GM = "pending_deputy_gm", "待副总经理审批"
        PENDING_GM = "pending_gm", "待总经理审批"
        PENDING_CHAIRMAN = "pending_chairman", "待董事长审批"
        APPROVED = "approved", "审批完成"
        ACCEPTING = "accepting", "验收中"
        SETTLED = "settled", "已结算"

    class RepairType(models.TextChoices):
        ELECTRICAL = "electrical", "电气维修"
        PLUMBING = "plumbing", "管道维修"
        CIVIL = "civil", "土建维修"
        EQUIPMENT = "equipment", "设备维修"
        RENOVATION = "renovation", "装修改造"
        OTHER = "other", "其他"

    class Urgency(models.TextChoices):
        NORMAL = "normal", "一般"
        URGENT = "urgent", "紧急"
        CRITICAL = "critical", "特急"

    code = models.CharField("维修单编号", max_length=50, unique=True)
    site = models.ForeignKey(
        Site,
        verbose_name="申请单位",
        related_name="repair_requests",
        on_delete=models.PROTECT,
    )
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="申请人",
        related_name="repair_requests",
        on_delete=models.CASCADE,
    )
    repair_type = models.CharField(
        "维修类型",
        max_length=20,
        choices=RepairType.choices,
        default=RepairType.OTHER,
    )
    urgency = models.CharField(
        "紧急程度",
        max_length=10,
        choices=Urgency.choices,
        default=Urgency.NORMAL,
    )
    equipment_name = models.CharField("设备名称", max_length=200, blank=True)
    equipment = models.ForeignKey(
        Equipment,
        verbose_name="关联设备档案",
        related_name="repair_requests",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        help_text="扫码报修或手动选择后，单据自动归入设备履历",
    )
    description = models.TextField("问题描述")
    budget_amount = models.DecimalField(
        "预算总额", max_digits=14, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(
        "当前状态",
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    parent = models.ForeignKey(
        "self",
        verbose_name="追加来源单据",
        related_name="appendages",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        help_text="本单为追加单时，关联原始维修单",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "维修单"
        verbose_name_plural = "维修单"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"], name="repair_status_idx"),
        ]

    @classmethod
    def generate_code(cls, site, at=None):
        """生成编号：油站编码 + 年月 + 3位序号，按油站、年月分桶递增。"""
        at = at or timezone.now()
        year_month = at.strftime("%Y%m")
        prefix = f"{site.code}{year_month}"
        last = (
            cls.objects.select_for_update()
            .filter(code__startswith=prefix)
            .order_by("-code")
            .first()
        )
        sequence = 1
        if last is not None:
            try:
                sequence = int(last.code[-3:]) + 1
            except ValueError:
                sequence = cls.objects.filter(code__startswith=prefix).count() + 1
        return f"{prefix}{sequence:03d}"

    def save(self, *args, **kwargs):
        if not self.code and self.site_id:
            with transaction.atomic():
                self.code = self.generate_code(self.site)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def __str__(self):
        if self.code:
            return f"{self.code} - {self.get_status_display()}"
        return f"{self.site.name} - {self.get_status_display()} - {self.created_at:%Y-%m-%d}"


class ApprovalRecord(models.Model):
    """审批记录"""

    class Result(models.TextChoices):
        APPROVED = "approved", "通过"
        REJECTED = "rejected", "驳回"

    class Node(models.TextChoices):
        APPLICATION = "application", "申请审批"
        DISPATCH = "dispatch", "派工审批"
        SETTLEMENT = "settlement", "结算审批"

    repair_request = models.ForeignKey(
        RepairRequest,
        verbose_name="维修单",
        related_name="approval_records",
        on_delete=models.CASCADE,
    )
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="审批人",
        related_name="approval_records",
        on_delete=models.PROTECT,
    )
    node = models.CharField(
        "审批节点",
        max_length=20,
        choices=Node.choices,
        default=Node.APPLICATION,
    )
    step = models.CharField(
        "审批环节",
        max_length=30,
        blank=True,
        choices=RepairRequest.Status.choices,
        help_text="申请审批链中具体到哪一级（片区管理员/经理/安数部/副总/总经理/董事长）",
    )
    result = models.CharField("审批结果", max_length=10, choices=Result.choices)
    comment = models.TextField("审批意见", blank=True)
    approved_at = models.DateTimeField("审批时间", auto_now_add=True)

    class Meta:
        verbose_name = "审批记录"
        verbose_name_plural = "审批记录"
        ordering = ["-approved_at"]

    def __str__(self):
        return f"维修单#{self.repair_request_id} - {self.get_node_display()} - {self.approver} - {self.get_result_display()}"


class RepairMaterialItem(models.Model):
    """维修单材料明细"""

    repair_request = models.ForeignKey(
        RepairRequest,
        verbose_name="维修单",
        related_name="material_items",
        on_delete=models.CASCADE,
    )
    material = models.ForeignKey(
        Material,
        verbose_name="标准化材料",
        related_name="material_items",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    custom_name = models.CharField("自定义材料名称", max_length=200, blank=True)
    quantity = models.DecimalField("数量", max_digits=12, decimal_places=2)
    unit_price = models.DecimalField("单价", max_digits=12, decimal_places=2)
    subtotal = models.DecimalField("小计", max_digits=14, decimal_places=2, default=0)

    class Meta:
        verbose_name = "维修单材料明细"
        verbose_name_plural = "维修单材料明细"

    def __str__(self):
        return f"{self.material_name} × {self.quantity}"

    @property
    def material_name(self):
        if self.material_id:
            return self.material.name
        return self.custom_name

    def save(self, *args, **kwargs):
        self.subtotal = self.quantity * self.unit_price
        super().save(*args, **kwargs)


class EngineeringTeam(models.Model):
    """工程队"""

    class QualificationLevel(models.TextChoices):
        SPECIAL = "special", "特级"
        LEVEL_1 = "level_1", "一级"
        LEVEL_2 = "level_2", "二级"
        LEVEL_3 = "level_3", "三级"
        LEVEL_4 = "level_4", "四级"

    code = models.CharField("工程队编码", max_length=50, unique=True)
    full_name = models.CharField("全称", max_length=200)
    credit_code = models.CharField("统一社会信用代码", max_length=18, unique=True)
    legal_person = models.CharField("法定代表人", max_length=50)
    bank_account = models.CharField("银行账户", max_length=50, blank=True)
    qualification_level = models.CharField(
        "资质等级",
        max_length=10,
        choices=QualificationLevel.choices,
        blank=True,
    )
    bound_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="绑定用户",
        related_name="engineering_team",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "工程队"
        verbose_name_plural = "工程队"
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name


class UserProfile(models.Model):
    """用户扩展信息"""

    class Role(models.TextChoices):
        SITE_MANAGER = "site_manager", "油站站长"
        REGION_ADMIN = "region_admin", "片区管理员"
        REGION_MANAGER = "region_manager", "片区经理"
        SAFETY_REPAIR = "safety_repair", "安数部维修岗"
        DEPUTY_GM = "deputy_gm", "副总经理"
        GM = "gm", "总经理"
        CHAIRMAN = "chairman", "董事长"
        TEAM_LEADER = "team_leader", "工程队负责人"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        related_name="profile",
        on_delete=models.CASCADE,
    )
    role = models.CharField(
        "角色",
        max_length=20,
        choices=Role.choices,
        default=Role.SITE_MANAGER,
    )
    region = models.ForeignKey(
        Region,
        verbose_name="所属片区",
        related_name="user_profiles",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    site = models.ForeignKey(
        Site,
        verbose_name="所属油站",
        related_name="user_profiles",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "用户扩展"
        verbose_name_plural = "用户扩展"

    def __str__(self):
        return f"{self.user.username}（{self.get_role_display()}）"


class DispatchOrder(models.Model):
    """派工单"""

    class Status(models.TextChoices):
        PENDING_ACCEPT = "pending_accept", "待接单"
        IN_PROGRESS = "in_progress", "维修中"
        ACCEPTING = "accepting", "验收中"
        ADMIN_REVIEW = "admin_review", "管理员审核"
        SAFETY_REVIEW = "safety_review", "安数部审核"
        CLOSED = "closed", "验收闭环"
        REJECTED = "rejected", "已拒单"

    code = models.CharField("派工单编号", max_length=50, unique=True, blank=True)
    repair_request = models.OneToOneField(
        RepairRequest,
        verbose_name="维修单",
        related_name="dispatch_order",
        on_delete=models.PROTECT,
    )
    engineering_team = models.ForeignKey(
        EngineeringTeam,
        verbose_name="工程队",
        related_name="dispatch_orders",
        on_delete=models.PROTECT,
    )
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="负责人",
        related_name="led_dispatch_orders",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    content = models.TextField("维修内容", blank=True)
    started_at = models.DateTimeField("接单/开工时间", null=True, blank=True)
    completed_at = models.DateTimeField("完成时间", null=True, blank=True)
    work_hours = models.DecimalField(
        "维修工时（小时）",
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
    )
    actual_fault = models.TextField("实际故障情况", blank=True)
    repair_plan = models.TextField("维修方案", blank=True)
    actual_measures = models.TextField("实际维修措施", blank=True)
    reject_reason = models.CharField("拒单理由", max_length=255, blank=True)
    reassigned_times = models.PositiveIntegerField("改派次数", default=0)
    photo = models.ImageField("现场照片", upload_to="dispatch_photos/", blank=True)
    status = models.CharField(
        "状态",
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING_ACCEPT,
    )
    is_rejected = models.BooleanField("拒收标识", default=False)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "派工单"
        verbose_name_plural = "派工单"
        ordering = ["-created_at"]

    @classmethod
    def generate_code(cls, team, at=None):
        """生成编号：PGD_工程队编码 + 年月 + 3位序号。"""
        at = at or timezone.now()
        prefix = f"PGD_{team.code}{at.strftime('%Y%m')}"
        last = (
            cls.objects.select_for_update()
            .filter(code__startswith=prefix)
            .order_by("-code")
            .first()
        )
        sequence = 1
        if last is not None:
            try:
                sequence = int(last.code[-3:]) + 1
            except ValueError:
                sequence = cls.objects.filter(code__startswith=prefix).count() + 1
        return f"{prefix}{sequence:03d}"

    def save(self, *args, **kwargs):
        if not self.code and self.engineering_team_id:
            with transaction.atomic():
                self.code = self.generate_code(self.engineering_team)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code}（{self.repair_request.code or self.repair_request_id}）"

    @property
    def elapsed_hours(self):
        """接单到完工的实际耗时（小时，两位小数）；未完单返回 None。"""
        if self.started_at and self.completed_at:
            return round((self.completed_at - self.started_at).total_seconds() / 3600, 2)
        return None


class DispatchMedia(models.Model):
    """派工单现场媒体（照片/视频，一对多）"""

    class MediaType(models.TextChoices):
        IMAGE = "image", "照片"
        VIDEO = "video", "视频"

    class Phase(models.TextChoices):
        BEFORE = "before", "维修前"
        AFTER = "after", "维修后"

    IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
    VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}
    MAX_IMAGE_SIZE = 20 * 1024 * 1024       # 单张照片 20MB
    MAX_VIDEO_SIZE = 500 * 1024 * 1024      # 单个视频 500MB

    dispatch_order = models.ForeignKey(
        DispatchOrder,
        verbose_name="派工单",
        related_name="media_files",
        on_delete=models.CASCADE,
    )
    media_type = models.CharField("类型", max_length=10, choices=MediaType.choices)
    phase = models.CharField(
        "维修阶段",
        max_length=10,
        choices=Phase.choices,
        default=Phase.AFTER,
    )
    file = models.FileField("文件", upload_to="dispatch_media/%Y/%m/")
    filename = models.CharField("原始文件名", max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="上传人",
        related_name="dispatch_media",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField("上传时间", auto_now_add=True)

    class Meta:
        verbose_name = "派工现场媒体"
        verbose_name_plural = "派工现场媒体"
        ordering = ["created_at", "id"]

    @classmethod
    def detect_type(cls, filename):
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in cls.IMAGE_EXTENSIONS:
            return cls.MediaType.IMAGE
        if ext in cls.VIDEO_EXTENSIONS:
            return cls.MediaType.VIDEO
        return None

    def __str__(self):
        return f"{self.dispatch_order.code} - {self.get_media_type_display()} - {self.filename or self.file.name}"


class AcceptanceRating(models.Model):
    """验收评价：报修人对完工服务三维度打分（1-5 星）。"""

    dispatch_order = models.OneToOneField(
        DispatchOrder,
        verbose_name="派工单",
        related_name="rating",
        on_delete=models.CASCADE,
    )
    response_speed = models.PositiveSmallIntegerField("响应速度", default=5)
    technical_level = models.PositiveSmallIntegerField("技术水平", default=5)
    service_attitude = models.PositiveSmallIntegerField("服务态度", default=5)
    comment = models.TextField("评价内容", blank=True)
    rater = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="评价人",
        related_name="dispatch_ratings",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField("评价时间", auto_now_add=True)

    class Meta:
        verbose_name = "验收评价"
        verbose_name_plural = "验收评价"

    @property
    def average_score(self):
        return round(
            (self.response_speed + self.technical_level + self.service_attitude) / 3, 1
        )

    def __str__(self):
        return f"{self.dispatch_order.code} 评价 {self.average_score} 星"


class RepairMedia(models.Model):
    """报修现场媒体（提交报修单时上传的照片/视频，一对多）。"""

    class MediaType(models.TextChoices):
        IMAGE = "image", "照片"
        VIDEO = "video", "视频"

    # 与派工媒体保持同一套口径
    IMAGE_EXTENSIONS = DispatchMedia.IMAGE_EXTENSIONS
    VIDEO_EXTENSIONS = DispatchMedia.VIDEO_EXTENSIONS
    MAX_IMAGE_SIZE = DispatchMedia.MAX_IMAGE_SIZE
    MAX_VIDEO_SIZE = DispatchMedia.MAX_VIDEO_SIZE

    repair_request = models.ForeignKey(
        RepairRequest,
        verbose_name="维修单",
        related_name="media_files",
        on_delete=models.CASCADE,
    )
    media_type = models.CharField("类型", max_length=10, choices=MediaType.choices)
    file = models.FileField("文件", upload_to="repair_media/%Y/%m/")
    filename = models.CharField("原始文件名", max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="上传人",
        related_name="repair_media",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField("上传时间", auto_now_add=True)

    class Meta:
        verbose_name = "报修现场媒体"
        verbose_name_plural = "报修现场媒体"
        ordering = ["created_at", "id"]

    @classmethod
    def detect_type(cls, filename):
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in cls.IMAGE_EXTENSIONS:
            return cls.MediaType.IMAGE
        if ext in cls.VIDEO_EXTENSIONS:
            return cls.MediaType.VIDEO
        return None

    def __str__(self):
        return f"{self.repair_request.code} - {self.get_media_type_display()} - {self.filename or self.file.name}"


class TicketEvent(models.Model):
    """工单历程事件：每次状态/环节变更追加一条，只增不改，支撑审计时间线。"""

    class EventType(models.TextChoices):
        CREATED = "created", "提交报修"
        APPROVED = "approved", "审批通过"
        REJECTED = "rejected", "审批驳回"
        RESUBMITTED = "resubmitted", "重新提交"
        DISPATCHED = "dispatched", "任务派发"
        ACCEPTED = "accepted", "工程队接单"
        DISPATCH_REJECTED = "dispatch_rejected", "工程队拒单"
        REASSIGNED = "reassigned", "重新派单"
        COMPLETED = "completed", "完工上报"
        REVIEW_PASSED = "review_passed", "验收通过"
        REVIEW_REJECTED = "review_rejected", "验收驳回"
        CLOSED = "closed", "验收闭环"
        RATED = "rated", "验收评价"
        STOCK_USED = "stock_used", "备件领用"
        SETTLED = "settled", "结算完成"

    repair_request = models.ForeignKey(
        RepairRequest,
        verbose_name="维修单",
        related_name="events",
        on_delete=models.CASCADE,
    )
    event_type = models.CharField("事件类型", max_length=20, choices=EventType.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作人",
        related_name="ticket_events",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    detail = models.CharField("环节说明", max_length=255, blank=True)
    remark = models.TextField("意见备注", blank=True)
    created_at = models.DateTimeField("发生时间", auto_now_add=True)

    class Meta:
        verbose_name = "工单历程事件"
        verbose_name_plural = "工单历程事件"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["event_type"], name="ticketevent_type_idx")]

    def save(self, *args, **kwargs):
        # 历程事件只允许新增（INSERT），禁止 UPDATE，保证不可篡改
        if self.pk:
            raise ValueError("工单历程事件为不可变记录，不允许修改")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.repair_request.code} - {self.get_event_type_display()} - {self.created_at:%Y-%m-%d %H:%M}"


class SettlementOrder(models.Model):
    """结算单"""

    # 安数部编码，可在 settings 中通过 SAFETY_DEPT_CODE 覆盖
    DEPT_CODE_SETTING = "SAFETY_DEPT_CODE"
    DEFAULT_DEPT_CODE = "ASB"

    code = models.CharField("结算单编号", max_length=50, unique=True, blank=True)
    dispatch_order = models.ForeignKey(
        DispatchOrder,
        verbose_name="派工单",
        related_name="settlement_orders",
        on_delete=models.PROTECT,
    )
    amount = models.DecimalField("结算金额", max_digits=14, decimal_places=2)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        related_name="settlement_orders",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    is_settled = models.BooleanField("是否已结算", default=False)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "结算单"
        verbose_name_plural = "结算单"
        ordering = ["-created_at"]

    @classmethod
    def get_dept_code(cls):
        return getattr(settings, cls.DEPT_CODE_SETTING, cls.DEFAULT_DEPT_CODE)

    @classmethod
    def generate_code(cls, at=None):
        """生成编号：JSD_安数部编码 + 年月 + 3位序号。"""
        at = at or timezone.now()
        prefix = f"JSD_{cls.get_dept_code()}{at.strftime('%Y%m')}"
        last = (
            cls.objects.select_for_update()
            .filter(code__startswith=prefix)
            .order_by("-code")
            .first()
        )
        sequence = 1
        if last is not None:
            try:
                sequence = int(last.code[-3:]) + 1
            except ValueError:
                sequence = cls.objects.filter(code__startswith=prefix).count() + 1
        return f"{prefix}{sequence:03d}"

    def save(self, *args, **kwargs):
        if not self.code:
            with transaction.atomic():
                self.code = self.generate_code()
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def __str__(self):
        state = "已结算" if self.is_settled else "未结算"
        return f"{self.code} - {state}"


class ApprovalFlowConfig(models.Model):
    """审批流配置（流程类型、节点超时阈值与预警推送角色）"""

    class FlowType(models.TextChoices):
        REPAIR_APPLICATION = "repair_application", "维修申请"
        DISPATCH = "dispatch", "派工验收"
        SETTLEMENT = "settlement", "结算流程"

    flow_type = models.CharField(
        "流程类型", max_length=20, choices=FlowType.choices, default=FlowType.REPAIR_APPLICATION
    )
    node = models.CharField(
        "节点名称",
        max_length=20,
        choices=ApprovalRecord.Node.choices,
    )
    timeout_days = models.PositiveIntegerField("超时阈值（天）", default=1)
    warn_before_days = models.PositiveIntegerField(
        "提前预警（天）", default=0, help_text="距超时多少天开始推送预警，0 表示当天预警"
    )
    warn_roles = models.CharField(
        "预警推送角色",
        max_length=100,
        blank=True,
        help_text="多个角色用英文逗号分隔，如：region_admin,region_manager",
    )
    is_active = models.BooleanField("启用", default=True)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "审批流配置"
        verbose_name_plural = "审批流配置"
        constraints = [
            models.UniqueConstraint(
                fields=["flow_type", "node"], name="uniq_flow_node"
            )
        ]

    def __str__(self):
        return f"{self.get_flow_type_display()} - {self.get_node_display()} - 超时{self.timeout_days}天"

    def get_warn_roles(self):
        """返回预警角色 key 列表"""
        return [r.strip() for r in self.warn_roles.split(",") if r.strip()]


class Notification(models.Model):
    """通知消息"""

    class Category(models.TextChoices):
        APPROVAL = "approval", "审批通知"
        WARNING = "warning", "超时预警"
        DISPATCH = "dispatch", "派工通知"
        SETTLEMENT = "settlement", "结算通知"
        STOCK = "stock", "库存预警"
        SYSTEM = "system", "系统通知"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="接收人",
        related_name="notifications",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    category = models.CharField(
        "消息类型", max_length=20, choices=Category.choices, default=Category.SYSTEM
    )
    title = models.CharField("标题", max_length=200)
    content = models.TextField("内容", blank=True)
    business_code = models.CharField("关联单据编号", max_length=50, blank=True)
    is_read = models.BooleanField("已读", default=False)
    read_at = models.DateTimeField("阅读时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "通知消息"
        verbose_name_plural = "通知消息"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["is_read"], name="notif_unread_idx"),
        ]

    def __str__(self):
        return f"{self.title}（{self.get_category_display()}）"


class OperationLog(models.Model):
    """操作日志"""

    class Result(models.TextChoices):
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作人",
        related_name="operation_logs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    module = models.CharField("功能模块", max_length=50)
    action = models.CharField("操作动作", max_length=100)
    content = models.TextField("操作内容", blank=True)
    ip = models.GenericIPAddressField("IP 地址", null=True, blank=True)
    result = models.CharField(
        "操作结果", max_length=10, choices=Result.choices, default=Result.SUCCESS
    )
    created_at = models.DateTimeField("操作时间", auto_now_add=True)

    class Meta:
        verbose_name = "操作日志"
        verbose_name_plural = "操作日志"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["module"], name="oplog_module_idx"),
        ]

    def __str__(self):
        return f"{self.module} - {self.action} - {self.get_result_display()}"


class Dictionary(models.Model):
    """系统字段字典（维修类型、紧急程度等）"""

    category = models.CharField("字典分类", max_length=50, db_index=True)
    code = models.CharField("字典编码", max_length=50)
    label = models.CharField("显示名称", max_length=100)
    sort_order = models.PositiveIntegerField("排序", default=0)
    is_active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "系统字典"
        verbose_name_plural = "系统字典"
        ordering = ["category", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "code"], name="uniq_dict_category_code"
            )
        ]

    def __str__(self):
        return f"{self.category}:{self.code} - {self.label}"


class CommonPhrase(models.Model):
    """审批常用语"""

    node = models.CharField(
        "适用节点",
        max_length=20,
        choices=ApprovalRecord.Node.choices,
        blank=True,
        help_text="留空表示所有节点通用",
    )
    content = models.CharField("常用语内容", max_length=255)
    sort_order = models.PositiveIntegerField("排序", default=0)
    is_active = models.BooleanField("启用", default=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        related_name="common_phrases",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "审批常用语"
        verbose_name_plural = "审批常用语"
        ordering = ["sort_order", "-created_at"]

    def __str__(self):
        return self.content
