"""派工现场媒体（多照片/视频）上传、设备档案、报修环节增强功能的单元测试。"""
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from .models import (
    AcceptanceRating,
    DispatchMedia,
    DispatchOrder,
    EngineeringTeam,
    Equipment,
    Notification,
    Region,
    RepairMedia,
    RepairRequest,
    Site,
    SparePart,
    StockMovement,
    TicketEvent,
    UserProfile,
)
from .services import apply_stock_movement

User = get_user_model()

# 最小合法 PNG（1x1）
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00"
    b"\x05\xfe\x02\xfe\xa3UvE\x00\x00\x00\x00IEND\xaeB`\x82"
)
# 带 ftyp 头的最小 MP4 占位内容
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"0" * 200


@override_settings(ALLOWED_HOSTS=["*"])
class DispatchMediaTypeTests(TestCase):
    """文件类型识别（不依赖数据库/文件系统）。"""

    def test_image_extensions(self):
        for name in ("a.jpg", "b.JPEG", "c.png", "d.gif", "e.webp", "f.bmp"):
            with self.subTest(name=name):
                self.assertEqual(
                    DispatchMedia.detect_type(name), DispatchMedia.MediaType.IMAGE
                )

    def test_video_extensions(self):
        for name in ("a.mp4", "b.MOV", "c.avi", "d.mkv", "e.webm", "f.m4v"):
            with self.subTest(name=name):
                self.assertEqual(
                    DispatchMedia.detect_type(name), DispatchMedia.MediaType.VIDEO
                )

    def test_unsupported_and_missing_extension(self):
        self.assertIsNone(DispatchMedia.detect_type("说明.txt"))
        self.assertIsNone(DispatchMedia.detect_type("noext"))
        self.assertIsNone(DispatchMedia.detect_type(""))


@override_settings(ALLOWED_HOSTS=["*"])
class DispatchMediaUploadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(code="UT-R", name="单测片区")
        cls.site = Site.objects.create(
            code="UT-SITE-001", name="单测油站", region=cls.region
        )
        cls.safety = User.objects.create_user("ut_safety", password="x")
        UserProfile.objects.create(user=cls.safety, role=UserProfile.Role.SAFETY_REPAIR)
        cls.radmin = User.objects.create_user("ut_radmin", password="x")
        UserProfile.objects.create(
            user=cls.radmin, role=UserProfile.Role.REGION_ADMIN, region=cls.region
        )
        cls.leader = User.objects.create_user("ut_leader", password="x")
        UserProfile.objects.create(user=cls.leader, role=UserProfile.Role.TEAM_LEADER)
        cls.team = EngineeringTeam.objects.create(
            code="UTGC",
            full_name="单测工程队",
            credit_code="UT0000000000000001",
            legal_person="测试法人",
            bound_user=cls.leader,
        )
        cls.repair = RepairRequest.objects.create(
            site=cls.site,
            reporter=cls.leader,
            repair_type=RepairRequest.RepairType.ELECTRICAL,
            urgency=RepairRequest.Urgency.NORMAL,
            description="媒体上传单测",
            status=RepairRequest.Status.APPROVED,
        )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="ut_media_")
        cls._override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def _create_dispatch(self):
        return DispatchOrder.objects.create(
            repair_request=self.repair,
            engineering_team=self.team,
            leader=self.leader,
            status=DispatchOrder.Status.IN_PROGRESS,
        )

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _image(self, name="photo.png"):
        return SimpleUploadedFile(name, PNG_BYTES, content_type="image/png")

    def _video(self, name="clip.mp4"):
        return SimpleUploadedFile(name, MP4_BYTES, content_type="video/mp4")

    def _complete(self, client, dispatch, after_files, before_files=None,
                  content="完工内容", **extra):
        data = {"content": content}
        data.update(extra)
        if after_files is not None:
            data["media"] = after_files
        if before_files is not None:
            data["before_media"] = before_files
        return client.post(
            f"/dispatch/{dispatch.pk}/complete/",
            data,
        )

    def test_multiple_images_and_video_upload(self):
        dispatch = self._create_dispatch()
        response = self._complete(
            self._login(self.leader),
            dispatch,
            after_files=[
                self._image("维修后1.png"),
                self._image("维修后2.JPG"),
                self._video("维修过程.mp4"),
            ],
            before_files=[self._image("维修前.png")],
            actual_fault="电源模块烧毁",
            repair_plan="更换电源模块",
            actual_measures="已更换并通电测试",
            work_hours="3.50",
        )

        self.assertEqual(response.status_code, 302)
        dispatch.refresh_from_db()
        media = list(dispatch.media_files.all())
        self.assertEqual(len(media), 4)
        self.assertCountEqual(
            [m.media_type for m in media],
            [DispatchMedia.MediaType.IMAGE, DispatchMedia.MediaType.IMAGE,
             DispatchMedia.MediaType.VIDEO, DispatchMedia.MediaType.IMAGE],
        )
        self.assertEqual(
            {m.filename for m in media},
            {"维修前.png", "维修后1.png", "维修后2.JPG", "维修过程.mp4"},
        )
        # 前/后阶段分组落库
        before = [m for m in media if m.phase == DispatchMedia.Phase.BEFORE]
        after = [m for m in media if m.phase == DispatchMedia.Phase.AFTER]
        self.assertEqual([m.filename for m in before], ["维修前.png"])
        self.assertEqual(
            {m.filename for m in after},
            {"维修后1.png", "维修后2.JPG", "维修过程.mp4"},
        )
        # 维修过程字段落库
        self.assertEqual(dispatch.actual_fault, "电源模块烧毁")
        self.assertEqual(dispatch.repair_plan, "更换电源模块")
        self.assertEqual(dispatch.actual_measures, "已更换并通电测试")
        self.assertEqual(str(dispatch.work_hours), "3.50")
        # 第一张维修后照片同步到旧单图字段
        self.assertTrue(dispatch.photo)
        # 单据进入验收中并记录完成时间
        self.assertEqual(dispatch.status, DispatchOrder.Status.ACCEPTING)
        self.assertIsNotNone(dispatch.completed_at)
        # 上传人被记录，文件真实落盘
        for m in media:
            self.assertEqual(m.uploaded_by, self.leader)
            self.assertTrue(m.file.storage.exists(m.file.name))
        # 前/后媒体在详情页分区渲染
        page = self._login(self.safety).get(f"/dispatch/{dispatch.pk}/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn("<video", html)
        self.assertIn("维修过程.mp4", html)
        self.assertIn("维修前.png", html)
        self.assertIn("维修前照片与视频", html)
        self.assertIn("维修后照片与视频", html)

    def test_invalid_file_type_rejected(self):
        dispatch = self._create_dispatch()
        bad = SimpleUploadedFile("说明.txt", b"hello", content_type="text/plain")

        response = self._complete(self._login(self.leader), dispatch, [bad])

        self.assertEqual(response.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)
        self.assertIsNone(dispatch.completed_at)
        self.assertEqual(dispatch.media_files.count(), 0)

    def test_oversized_file_rejected(self):
        dispatch = self._create_dispatch()
        # 将图片上限临时调为 10 字节，小 PNG 即超限，避免构造 20MB 文件
        with mock.patch.object(DispatchMedia, "MAX_IMAGE_SIZE", 10):
            response = self._complete(
                self._login(self.leader), dispatch, [self._image("big.png")]
            )

        self.assertEqual(response.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)
        self.assertEqual(dispatch.media_files.count(), 0)

    def test_empty_upload_rejected(self):
        dispatch = self._create_dispatch()
        response = self._complete(self._login(self.leader), dispatch, None)

        self.assertEqual(response.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)
        self.assertEqual(dispatch.media_files.count(), 0)

    def test_before_only_upload_rejected(self):
        """只传维修前媒体、缺少维修后媒体时不允许完工。"""
        dispatch = self._create_dispatch()
        response = self._complete(
            self._login(self.leader),
            dispatch,
            after_files=None,
            before_files=[self._image("维修前.png")],
        )

        self.assertEqual(response.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)
        self.assertEqual(dispatch.media_files.count(), 0)

    def test_non_team_member_cannot_complete(self):
        dispatch = self._create_dispatch()
        # 片区管理员不属于该工程队，完工上报应被拒绝
        response = self._complete(
            self._login(self.radmin), dispatch, [self._image()]
        )

        self.assertEqual(response.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)
        self.assertEqual(dispatch.media_files.count(), 0)

    def test_anonymous_redirected_to_login(self):
        dispatch = self._create_dispatch()
        response = self.client.post(
            f"/dispatch/{dispatch.pk}/complete/",
            {"content": "x", "media": [self._image()]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)
        self.assertEqual(dispatch.media_files.count(), 0)

    def test_append_media_after_rejection(self):
        dispatch = self._create_dispatch()
        leader_client = self._login(self.leader)

        # 第一次完工：1 张照片
        self._complete(leader_client, dispatch, [self._image("第一次.png")])
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.ACCEPTING)

        # 管理员拒收 -> 退回维修中
        self._login(self.radmin).post(
            f"/dispatch/{dispatch.pk}/review/",
            {"result": "rejected", "note": "角度不全"},
        )
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)
        self.assertTrue(dispatch.is_rejected)
        self.assertIsNone(dispatch.completed_at)
        self.assertEqual(dispatch.media_files.count(), 1)

        # 返工后再次完工：追加 1 个视频，历史媒体保留
        self._complete(leader_client, dispatch, [self._video("补充.mp4")])
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.ACCEPTING)
        self.assertFalse(dispatch.is_rejected)
        self.assertIsNotNone(dispatch.completed_at)
        self.assertEqual(dispatch.media_files.count(), 2)
        self.assertCountEqual(
            dispatch.media_files.values_list("media_type", flat=True),
            [DispatchMedia.MediaType.IMAGE, DispatchMedia.MediaType.VIDEO],
        )
        # 返工补传的媒体同样归入维修后
        self.assertTrue(
            all(m.phase == DispatchMedia.Phase.AFTER for m in dispatch.media_files.all())
        )


@override_settings(ALLOWED_HOSTS=["*"])
class DispatchExecutionEnhancementTests(TestCase):
    """接单/拒单/改派、维修过程字段、验收评价。"""

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(code="EX-R", name="执行增强片区")
        cls.site = Site.objects.create(code="EX-SITE", name="执行增强油站", region=cls.region)
        cls.safety = User.objects.create_user("ex_safety", password="x")
        UserProfile.objects.create(user=cls.safety, role=UserProfile.Role.SAFETY_REPAIR)
        cls.radmin = User.objects.create_user("ex_radmin", password="x")
        UserProfile.objects.create(
            user=cls.radmin, role=UserProfile.Role.REGION_ADMIN, region=cls.region
        )
        cls.reporter = User.objects.create_user("ex_reporter", password="x")
        UserProfile.objects.create(
            user=cls.reporter, role=UserProfile.Role.SITE_MANAGER, site=cls.site
        )
        cls.leader = User.objects.create_user("ex_leader", password="x")
        UserProfile.objects.create(user=cls.leader, role=UserProfile.Role.TEAM_LEADER)
        cls.leader2 = User.objects.create_user("ex_leader2", password="x")
        UserProfile.objects.create(user=cls.leader2, role=UserProfile.Role.TEAM_LEADER)
        cls.team1 = EngineeringTeam.objects.create(
            code="EXGC1",
            full_name="执行增强一队",
            credit_code="EX0000000000000001",
            legal_person="测试法人",
            bound_user=cls.leader,
        )
        cls.team2 = EngineeringTeam.objects.create(
            code="EXGC2",
            full_name="执行增强二队",
            credit_code="EX0000000000000002",
            legal_person="测试法人",
            bound_user=cls.leader2,
        )
        cls.equipment = Equipment.objects.create(site=cls.site, name="执行增强加油机")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="ut_exec_media_")
        cls._override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _approved_repair(self):
        return RepairRequest.objects.create(
            site=self.site,
            reporter=self.reporter,
            equipment=self.equipment,
            equipment_name=self.equipment.name,
            repair_type=RepairRequest.RepairType.ELECTRICAL,
            urgency=RepairRequest.Urgency.NORMAL,
            description="执行增强测试报修",
            status=RepairRequest.Status.APPROVED,
        )

    def _create_pending(self):
        repair = self._approved_repair()
        resp = self._login(self.safety).post("/dispatch/create/", {
            "repair_request": repair.pk,
            "engineering_team": self.team1.pk,
            "content": "上门维修",
        })
        self.assertEqual(resp.status_code, 302)
        dispatch = DispatchOrder.objects.get(repair_request=repair)
        return repair, dispatch

    def test_new_dispatch_is_pending_and_cannot_complete_before_accept(self):
        repair, dispatch = self._create_pending()
        repair.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.PENDING_ACCEPT)
        self.assertEqual(repair.status, RepairRequest.Status.ACCEPTING)
        # 待接单期间设备已按在途派工显示为维修中
        self.assertEqual(self.equipment.refresh_status(), Equipment.Status.IN_REPAIR)
        # 未接单直接完工被拒绝
        resp = self._login(self.leader).post(
            f"/dispatch/{dispatch.pk}/complete/",
            {"media": [SimpleUploadedFile("done.png", PNG_BYTES, content_type="image/png")]},
        )
        self.assertEqual(resp.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.PENDING_ACCEPT)
        self.assertEqual(dispatch.media_files.count(), 0)

    def test_team_member_accept_starts_work(self):
        _, dispatch = self._create_pending()
        # 非本工程队成员（片区管理员）不能接单
        resp = self._login(self.radmin).post(f"/dispatch/{dispatch.pk}/accept/")
        self.assertEqual(resp.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.PENDING_ACCEPT)

        # 本队负责人接单成功
        resp = self._login(self.leader).post(f"/dispatch/{dispatch.pk}/accept/")
        self.assertEqual(resp.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)
        self.assertIsNotNone(dispatch.started_at)
        self.assertTrue(
            dispatch.repair_request.events.filter(
                event_type=TicketEvent.EventType.ACCEPTED
            ).exists()
        )
        # 重复接单无效
        resp = self._login(self.leader).post(f"/dispatch/{dispatch.pk}/accept/")
        self.assertEqual(resp.status_code, 302)
        dispatch.refresh_from_db()
        self.assertIsNotNone(dispatch.started_at)

    def test_decline_requires_reason(self):
        _, dispatch = self._create_pending()
        resp = self._login(self.leader).post(
            f"/dispatch/{dispatch.pk}/decline/", {"reject_reason": ""}
        )
        self.assertEqual(resp.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.PENDING_ACCEPT)
        self.assertEqual(dispatch.reject_reason, "")

    def test_decline_then_reassign_and_reaccept(self):
        repair, dispatch = self._create_pending()
        # 其他工程队成员看不到他队派工单（可见性即隔离）
        resp = self._login(self.leader2).post(
            f"/dispatch/{dispatch.pk}/decline/", {"reject_reason": "不归我管"}
        )
        self.assertEqual(resp.status_code, 404)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.PENDING_ACCEPT)

        # 本队拒单：单据退回待派工，设备恢复故障
        resp = self._login(self.leader).post(
            f"/dispatch/{dispatch.pk}/decline/", {"reject_reason": "无高空作业资质"}
        )
        self.assertEqual(resp.status_code, 302)
        dispatch.refresh_from_db()
        repair.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.REJECTED)
        self.assertEqual(dispatch.reject_reason, "无高空作业资质")
        self.assertEqual(repair.status, RepairRequest.Status.APPROVED)
        self.assertEqual(self.equipment.refresh_status(), Equipment.Status.FAULT)
        self.assertTrue(
            repair.events.filter(
                event_type=TicketEvent.EventType.DISPATCH_REJECTED
            ).exists()
        )

        # 非安数部不能改派
        resp = self._login(self.radmin).post(
            f"/dispatch/{dispatch.pk}/reassign/", {"engineering_team": self.team2.pk}
        )
        self.assertEqual(resp.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.REJECTED)

        # 安数部改派二队：仍为同一条派工记录，重新待接单
        resp = self._login(self.safety).post(
            f"/dispatch/{dispatch.pk}/reassign/", {"engineering_team": self.team2.pk}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(DispatchOrder.objects.filter(repair_request=repair).count(), 1)
        dispatch.refresh_from_db()
        repair.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.PENDING_ACCEPT)
        self.assertEqual(dispatch.engineering_team_id, self.team2.pk)
        self.assertEqual(dispatch.leader_id, self.leader2.pk)
        self.assertEqual(dispatch.reassigned_times, 1)
        self.assertEqual(repair.status, RepairRequest.Status.ACCEPTING)
        self.assertEqual(self.equipment.refresh_status(), Equipment.Status.IN_REPAIR)
        self.assertTrue(
            repair.events.filter(
                event_type=TicketEvent.EventType.REASSIGNED
            ).exists()
        )

        # 旧队看不到改派后的单据，新队接单成功
        resp = self._login(self.leader).post(f"/dispatch/{dispatch.pk}/accept/")
        self.assertEqual(resp.status_code, 404)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.PENDING_ACCEPT)
        resp = self._login(self.leader2).post(f"/dispatch/{dispatch.pk}/accept/")
        self.assertEqual(resp.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)

    def _accept_and_complete(self, dispatch):
        client = self._login(self.leader)
        client.post(f"/dispatch/{dispatch.pk}/accept/")
        resp = client.post(
            f"/dispatch/{dispatch.pk}/complete/",
            {
                "content": "维修完成",
                "actual_fault": "主板故障",
                "repair_plan": "更换主板",
                "actual_measures": "更换后调试正常",
                "work_hours": "2.50",
                "before_media": [SimpleUploadedFile("before.png", PNG_BYTES, content_type="image/png")],
                "media": [SimpleUploadedFile("after.png", PNG_BYTES, content_type="image/png")],
            },
        )
        self.assertEqual(resp.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.ACCEPTING)

    def test_reporter_rating_after_completion(self):
        repair, dispatch = self._create_pending()
        # 完工前不能评价
        resp = self._login(self.reporter).post(
            f"/dispatch/{dispatch.pk}/rate/",
            {"response_speed": "5", "technical_level": "5", "service_attitude": "5"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(AcceptanceRating.objects.filter(dispatch_order=dispatch).exists())

        self._accept_and_complete(dispatch)

        # 非报修人不能评价
        resp = self._login(self.radmin).post(
            f"/dispatch/{dispatch.pk}/rate/",
            {"response_speed": "5", "technical_level": "5", "service_attitude": "5"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(AcceptanceRating.objects.filter(dispatch_order=dispatch).exists())

        # 超出 1-5 范围的打分无效
        resp = self._login(self.reporter).post(
            f"/dispatch/{dispatch.pk}/rate/",
            {"response_speed": "6", "technical_level": "3", "service_attitude": "5"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(AcceptanceRating.objects.filter(dispatch_order=dispatch).exists())

        # 正常评价
        resp = self._login(self.reporter).post(
            f"/dispatch/{dispatch.pk}/rate/",
            {
                "response_speed": "4",
                "technical_level": "3",
                "service_attitude": "5",
                "comment": "整体满意",
            },
        )
        self.assertEqual(resp.status_code, 302)
        rating = AcceptanceRating.objects.get(dispatch_order=dispatch)
        self.assertEqual((rating.response_speed, rating.technical_level,
                          rating.service_attitude), (4, 3, 5))
        self.assertEqual(rating.average_score, 4.0)
        self.assertEqual(rating.comment, "整体满意")
        self.assertEqual(rating.rater, self.reporter)
        self.assertTrue(
            repair.events.filter(event_type=TicketEvent.EventType.RATED).exists()
        )

        # 每单只能评价一次
        resp = self._login(self.reporter).post(
            f"/dispatch/{dispatch.pk}/rate/",
            {"response_speed": "1", "technical_level": "1", "service_attitude": "1"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AcceptanceRating.objects.filter(dispatch_order=dispatch).count(), 1)


# ===================== 设备档案（一物一码） =====================

class EquipmentModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(code="EQ-R", name="设备片区")
        cls.site = Site.objects.create(code="EQ-SITE", name="设备油站", region=cls.region)
        cls.reporter = User.objects.create_user("eq_reporter", password="x")
        cls.team = EngineeringTeam.objects.create(
            code="EQGC",
            full_name="设备单测工程队",
            credit_code="EQ0000000000000001",
            legal_person="测试法人",
        )

    def _equipment(self, name="1号加油机", **kwargs):
        return Equipment.objects.create(site=self.site, name=name, **kwargs)

    def test_code_autogenerated_sequentially(self):
        e1 = self._equipment("加油机A")
        e2 = self._equipment("加油机B")
        self.assertRegex(e1.code, r"^EQ\d{6}0001$")
        self.assertEqual(e2.code[-4:], "0002")
        self.assertTrue(e1.code[:8] == e2.code[:8])

    def test_expiry_alerts(self):
        today = date.today()
        eq = self._equipment(
            warranty_expiry=today + timedelta(days=10),   # 临期
            inspection_expiry=today - timedelta(days=3),  # 已逾期
            next_maintenance_date=today + timedelta(days=200),  # 不提醒
        )
        alerts = eq.expiry_alerts()
        labels = {a["label"]: a for a in alerts}
        self.assertIn("保修到期", labels)
        self.assertEqual(labels["保修到期"]["days_left"], 10)
        self.assertIn("检验到期", labels)
        self.assertEqual(labels["检验到期"]["overdue_days"], 3)
        self.assertNotIn("保养到期", labels)

    def test_refresh_status_follows_ticket_lifecycle(self):
        eq = self._equipment()
        self.assertEqual(eq.status, Equipment.Status.NORMAL)

        # 提交维修单 -> 故障
        repair = RepairRequest.objects.create(
            site=self.site,
            reporter=self.reporter,
            equipment=eq,
            equipment_name=eq.name,
            description="不出油",
            status=RepairRequest.Status.PENDING_REGION_ADMIN,
        )
        self.assertEqual(eq.refresh_status(), Equipment.Status.FAULT)

        # 派工维修 -> 维修中
        dispatch = DispatchOrder.objects.create(
            repair_request=repair,
            engineering_team=self.team,
            status=DispatchOrder.Status.IN_PROGRESS,
        )
        self.assertEqual(eq.refresh_status(), Equipment.Status.IN_REPAIR)

        # 验收闭环（维修单仍为 accepting 待结算）-> 恢复正常
        dispatch.status = DispatchOrder.Status.CLOSED
        dispatch.save(update_fields=["status"])
        repair.status = RepairRequest.Status.ACCEPTING
        repair.save(update_fields=["status"])
        self.assertEqual(eq.refresh_status(), Equipment.Status.NORMAL)

    def test_deactivated_equipment_not_auto_revived(self):
        eq = self._equipment()
        eq.status = Equipment.Status.DEACTIVATED
        eq.save(update_fields=["status"])
        RepairRequest.objects.create(
            site=self.site,
            reporter=self.reporter,
            equipment=eq,
            description="停用设备的新单",
            status=RepairRequest.Status.PENDING_REGION_ADMIN,
        )
        self.assertEqual(eq.refresh_status(), Equipment.Status.DEACTIVATED)


@override_settings(ALLOWED_HOSTS=["*"])
class EquipmentViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(code="EV-R", name="设备视图片区")
        cls.site1 = Site.objects.create(code="EV-S1", name="一站", region=cls.region)
        cls.site2 = Site.objects.create(code="EV-S2", name="二站", region=cls.region)
        cls.safety = User.objects.create_user("ev_safety", password="x")
        UserProfile.objects.create(user=cls.safety, role=UserProfile.Role.SAFETY_REPAIR)
        cls.manager1 = User.objects.create_user("ev_m1", password="x")
        UserProfile.objects.create(
            user=cls.manager1, role=UserProfile.Role.SITE_MANAGER, site=cls.site1
        )
        cls.manager2 = User.objects.create_user("ev_m2", password="x")
        UserProfile.objects.create(
            user=cls.manager2, role=UserProfile.Role.SITE_MANAGER, site=cls.site2
        )
        cls.team_leader = User.objects.create_user("ev_leader", password="x")
        UserProfile.objects.create(user=cls.team_leader, role=UserProfile.Role.TEAM_LEADER)
        cls.equip1 = Equipment.objects.create(
            site=cls.site1, name="一站1号加油机",
            category=Equipment.Category.FUEL_DISPENSER, location="前庭",
        )
        cls.equip2 = Equipment.objects.create(
            site=cls.site2, name="二站液位仪",
            category=Equipment.Category.LEVEL_GAUGE,
        )

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_anonymous_redirected_to_login(self):
        response = self.client.get("/equipment/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_list_visibility_scoped_by_site(self):
        html1 = self._login(self.manager1).get("/equipment/").content.decode()
        self.assertIn("一站1号加油机", html1)
        self.assertNotIn("二站液位仪", html1)

        html_safety = self._login(self.safety).get("/equipment/").content.decode()
        self.assertIn("一站1号加油机", html_safety)
        self.assertIn("二站液位仪", html_safety)

    def test_detail_shows_repair_history(self):
        RepairRequest.objects.create(
            site=self.site1,
            reporter=self.manager1,
            equipment=self.equip1,
            equipment_name=self.equip1.name,
            description="历史故障",
            status=RepairRequest.Status.SETTLED,
        )
        html = self._login(self.manager1).get(f"/equipment/{self.equip1.pk}/").content.decode()
        self.assertIn("维修履历", html)
        self.assertIn("历史故障", html)

    def test_manager_creates_equipment_for_own_site(self):
        client = self._login(self.manager1)
        response = client.post("/equipment/add/", {
            "site": self.site1.pk,
            "name": "一站消防栓",
            "category": Equipment.Category.FIRE_FIGHTING,
            "model_spec": "XF-100",
            "status": Equipment.Status.NORMAL,
        })
        self.assertEqual(response.status_code, 302)
        created = Equipment.objects.get(name="一站消防栓")
        self.assertEqual(created.site_id, self.site1.pk)
        self.assertTrue(created.code.startswith("EQ"))

    def test_manager_cannot_create_for_other_site(self):
        before = Equipment.objects.count()
        response = self._login(self.manager1).post("/equipment/add/", {
            "site": self.site2.pk,
            "name": "越权设备",
            "category": Equipment.Category.OTHER,
            "status": Equipment.Status.NORMAL,
        })
        self.assertEqual(response.status_code, 200)  # 表单校验失败，重新渲染
        self.assertEqual(Equipment.objects.count(), before)

    def test_team_leader_has_no_add_permission(self):
        response = self._login(self.team_leader).get("/equipment/add/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/equipment/")

    def test_qr_image_returns_png(self):
        response = self._login(self.manager1).get(f"/equipment/{self.equip1.pk}/qr.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response.content[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreater(len(response.content), 100)

    def test_qr_print_page(self):
        html = self._login(self.manager1).get(
            f"/equipment/{self.equip1.pk}/qr/"
        ).content.decode()
        self.assertIn("一站1号加油机", html)
        self.assertIn(f"/equipment/{self.equip1.pk}/qr.png", html)

    def test_scan_repair_prefills_equipment(self):
        html = self._login(self.manager1).get(
            f"/repair/add/?equipment={self.equip1.pk}"
        ).content.decode()
        self.assertIn("一站1号加油机", html)
        self.assertIn('value="%d" selected' % self.equip1.pk, html)

    def test_submit_repair_marks_equipment_fault_and_syncs_name(self):
        client = self._login(self.manager1)
        response = client.post("/repair/add/", {
            "site": self.site1.pk,
            "equipment": self.equip1.pk,
            "repair_type": RepairRequest.RepairType.EQUIPMENT,
            "urgency": RepairRequest.Urgency.URGENT,
            "description": "油枪不出油",
            # 空材料 formset
            "form-TOTAL_FORMS": "0",
            "form-INITIAL_FORMS": "0",
        })
        self.assertEqual(response.status_code, 302)
        self.equip1.refresh_from_db()
        self.assertEqual(self.equip1.status, Equipment.Status.FAULT)
        repair = RepairRequest.objects.get(equipment=self.equip1)
        self.assertEqual(repair.equipment_name, "一站1号加油机")


# ===================== 报修环节增强：现场附件 / 快速通道 / 工单历程 =====================

@override_settings(ALLOWED_HOSTS=["*"])
class RepairMediaUploadTests(TestCase):
    """提交报修单时上传现场照片/视频。"""

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(code="RM-R", name="报修媒体片区")
        cls.site = Site.objects.create(code="RM-SITE", name="报修媒体油站", region=cls.region)
        cls.reporter = User.objects.create_user("rm_reporter", password="x")
        UserProfile.objects.create(
            user=cls.reporter, role=UserProfile.Role.SITE_MANAGER, site=cls.site
        )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="ut_repair_media_")
        cls._override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def _login(self):
        client = Client()
        client.force_login(self.reporter)
        return client

    def _submit(self, client, files=None, urgency=RepairRequest.Urgency.NORMAL):
        data = {
            "site": self.site.pk,
            "repair_type": RepairRequest.RepairType.ELECTRICAL,
            "urgency": urgency,
            "description": "报修附件单测：配电箱异响",
            "form-TOTAL_FORMS": "0",
            "form-INITIAL_FORMS": "0",
        }
        if files is not None:
            data["media"] = files
        return client.post("/repair/add/", data)

    def test_submit_with_images_and_video(self):
        client = self._login()
        files = [
            SimpleUploadedFile("现场1.png", PNG_BYTES, content_type="image/png"),
            SimpleUploadedFile("现场2.JPG", PNG_BYTES, content_type="image/jpeg"),
            SimpleUploadedFile("视频.mp4", MP4_BYTES, content_type="video/mp4"),
        ]
        response = self._submit(client, files=files)
        self.assertEqual(response.status_code, 302)

        repair = RepairRequest.objects.get(description="报修附件单测：配电箱异响")
        media = list(repair.media_files.order_by("id"))
        self.assertEqual(len(media), 3)
        self.assertEqual(
            [m.media_type for m in media],
            [RepairMedia.MediaType.IMAGE, RepairMedia.MediaType.IMAGE, RepairMedia.MediaType.VIDEO],
        )
        self.assertEqual([m.filename for m in media], ["现场1.png", "现场2.JPG", "视频.mp4"])
        self.assertTrue(all(m.uploaded_by_id == self.reporter.id for m in media))
        # 文件真实落盘
        for m in media:
            self.assertTrue(m.file.storage.exists(m.file.name))
            self.assertTrue(m.file.name.replace("\\", "/").startswith("repair_media/"))
        # 提交即产生历程事件
        event = repair.events.get(event_type=TicketEvent.EventType.CREATED)
        self.assertEqual(event.actor_id, self.reporter.id)
        # 详情页渲染画廊与视频
        html = client.get(f"/repair/{repair.pk}/").content.decode()
        self.assertIn("报修现场照片与视频", html)
        self.assertIn("<video", html)

    def test_invalid_type_rejects_whole_ticket(self):
        before = RepairRequest.objects.count()
        response = self._submit(
            self._login(),
            files=[SimpleUploadedFile("说明.txt", b"hello", content_type="text/plain")],
        )
        self.assertEqual(response.status_code, 200)  # 重新渲染表单，不跳转
        self.assertEqual(RepairRequest.objects.count(), before)
        self.assertEqual(RepairMedia.objects.count(), 0)

    def test_oversized_file_rejected(self):
        big_png = SimpleUploadedFile("big.png", PNG_BYTES, content_type="image/png")
        with mock.patch.object(RepairMedia, "MAX_IMAGE_SIZE", 10):
            response = self._submit(self._login(), files=[big_png])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RepairMedia.objects.count(), 0)
        self.assertFalse(
            RepairRequest.objects.filter(description="报修附件单测：配电箱异响").exists()
        )

    def test_media_optional(self):
        response = self._submit(self._login())
        self.assertEqual(response.status_code, 302)
        repair = RepairRequest.objects.get(description="报修附件单测：配电箱异响")
        self.assertEqual(repair.media_files.count(), 0)

    def test_anonymous_redirected_to_login(self):
        response = self.client.post("/repair/add/", {})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


@override_settings(ALLOWED_HOSTS=["*"])
class FastTrackAndEventTests(TestCase):
    """特急快速通道路由 + 工单历程事件埋点。"""

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(code="FT-R", name="快速通道片区")
        cls.site = Site.objects.create(code="FT-SITE", name="快速通道油站", region=cls.region)
        cls.reporter = User.objects.create_user("ft_reporter", password="x")
        UserProfile.objects.create(
            user=cls.reporter, role=UserProfile.Role.SITE_MANAGER, site=cls.site
        )
        cls.radmin = User.objects.create_user("ft_radmin", password="x")
        UserProfile.objects.create(
            user=cls.radmin, role=UserProfile.Role.REGION_ADMIN, region=cls.region
        )
        cls.safety = User.objects.create_user("ft_safety", password="x")
        UserProfile.objects.create(user=cls.safety, role=UserProfile.Role.SAFETY_REPAIR)
        cls.root = User.objects.create_superuser("ft_root", password="x")
        cls.team = EngineeringTeam.objects.create(
            code="FTGC",
            full_name="快速通道工程队",
            credit_code="FT0000000000000001",
            legal_person="测试法人",
        )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="ut_fasttrack_media_")
        cls._override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _submit(self, client, urgency):
        return client.post("/repair/add/", {
            "site": self.site.pk,
            "repair_type": RepairRequest.RepairType.ELECTRICAL,
            "urgency": urgency,
            "description": f"紧急度 {urgency} 的报修单",
            "form-TOTAL_FORMS": "0",
            "form-INITIAL_FORMS": "0",
        })

    def test_normal_urgency_enters_region_admin(self):
        response = self._submit(self._login(self.reporter), RepairRequest.Urgency.NORMAL)
        repair = RepairRequest.objects.get(pk=response.url.rstrip("/").split("/")[-1])
        self.assertEqual(repair.status, RepairRequest.Status.PENDING_REGION_ADMIN)
        # 通知给片区管理员，不给安数部
        self.assertTrue(
            Notification.objects.filter(recipient=self.radmin, business_code=repair.code).exists()
        )
        self.assertFalse(
            Notification.objects.filter(recipient=self.safety, business_code=repair.code).exists()
        )
        event = repair.events.get(event_type=TicketEvent.EventType.CREATED)
        self.assertIn("片区管理员", event.detail)

    def test_urgent_still_uses_normal_chain(self):
        response = self._submit(self._login(self.reporter), RepairRequest.Urgency.URGENT)
        repair = RepairRequest.objects.get(pk=response.url.rstrip("/").split("/")[-1])
        self.assertEqual(repair.status, RepairRequest.Status.PENDING_REGION_ADMIN)

    def test_critical_skips_two_levels_to_safety(self):
        response = self._submit(self._login(self.reporter), RepairRequest.Urgency.CRITICAL)
        repair = RepairRequest.objects.get(pk=response.url.rstrip("/").split("/")[-1])
        self.assertEqual(repair.status, RepairRequest.Status.PENDING_SAFETY_REPAIR)
        self.assertTrue(
            Notification.objects.filter(recipient=self.safety, business_code=repair.code).exists()
        )
        self.assertFalse(
            Notification.objects.filter(recipient=self.radmin, business_code=repair.code).exists()
        )
        event = repair.events.get(event_type=TicketEvent.EventType.CREATED)
        self.assertIn("快速通道", event.detail)
        # 特急单直接出现在安数部审批待办
        html = self._login(self.safety).get("/approval/").content.decode()
        self.assertIn(repair.code, html)

    def test_reject_then_critical_resubmit_uses_fast_track(self):
        # 特急单提交 -> 安数部驳回 -> 重新提交仍走快速通道
        response = self._submit(self._login(self.reporter), RepairRequest.Urgency.CRITICAL)
        repair_id = response.url.rstrip("/").split("/")[-1]
        reject = self._login(self.root).post(
            f"/repair/{repair_id}/approve/",
            {"result": "rejected", "comment": "资料不全，补充后重提"},
        )
        self.assertEqual(reject.status_code, 302)
        repair = RepairRequest.objects.get(pk=repair_id)
        self.assertEqual(repair.status, RepairRequest.Status.DRAFT)
        self.assertTrue(
            repair.events.filter(event_type=TicketEvent.EventType.REJECTED).exists()
        )

        resubmit = self._login(self.reporter).post(f"/repair/{repair_id}/resubmit/")
        self.assertEqual(resubmit.status_code, 302)
        repair.refresh_from_db()
        self.assertEqual(repair.status, RepairRequest.Status.PENDING_SAFETY_REPAIR)
        self.assertTrue(
            repair.events.filter(event_type=TicketEvent.EventType.RESUBMITTED).exists()
        )

    def test_full_lifecycle_records_complete_event_chain(self):
        """特急单从提交到结算的完整事件序列（快速通道 4 级审批）。"""
        # 1) 提交
        response = self._submit(self._login(self.reporter), RepairRequest.Urgency.CRITICAL)
        repair_id = response.url.rstrip("/").split("/")[-1]
        root = self._login(self.root)

        # 2) 安数部 -> 副总 -> 总经理 -> 董事长，4 次审批通过
        for _ in range(4):
            approve = root.post(
                f"/repair/{repair_id}/approve/",
                {"result": "approved", "comment": "同意"},
            )
            self.assertEqual(approve.status_code, 302)
        repair = RepairRequest.objects.get(pk=repair_id)
        self.assertEqual(repair.status, RepairRequest.Status.APPROVED)

        # 3) 派工
        dispatch_resp = root.post("/dispatch/create/", {
            "repair_request": repair_id,
            "engineering_team": self.team.pk,
            "content": "立即上门",
        })
        self.assertEqual(dispatch_resp.status_code, 302)
        repair.refresh_from_db()
        self.assertEqual(repair.status, RepairRequest.Status.ACCEPTING)
        dispatch = DispatchOrder.objects.get(repair_request_id=repair_id)
        self.assertEqual(dispatch.status, DispatchOrder.Status.PENDING_ACCEPT)

        # 3.1) 工程队接单（root 为超级用户，放行通过）
        accept = root.post(f"/dispatch/{dispatch.pk}/accept/")
        self.assertEqual(accept.status_code, 302)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)
        self.assertIsNotNone(dispatch.started_at)

        # 4) 完工上报
        complete = root.post(
            f"/dispatch/{dispatch.pk}/complete/",
            {
                "content": "已更换配件",
                "media": [SimpleUploadedFile("done.png", PNG_BYTES, content_type="image/png")],
            },
        )
        self.assertEqual(complete.status_code, 302)

        # 5) 片区管理员验收通过 -> 安数部闭环
        review = root.post(f"/dispatch/{dispatch.pk}/review/", {"result": "approved"})
        self.assertEqual(review.status_code, 302)
        final = root.post(f"/dispatch/{dispatch.pk}/final-review/", {"result": "approved"})
        self.assertEqual(final.status_code, 302)

        # 6) 结算创建并确认
        settle = root.post(f"/dispatch/{dispatch.pk}/settle/", {"amount": "888.00"})
        self.assertEqual(settle.status_code, 302)
        settlement = dispatch.settlement_orders.first()
        confirm = root.post(f"/settlement/{settlement.pk}/confirm/")
        self.assertEqual(confirm.status_code, 302)

        types = list(
            TicketEvent.objects.filter(repair_request_id=repair_id)
            .order_by("id")
            .values_list("event_type", flat=True)
        )
        self.assertEqual(
            types,
            [
                TicketEvent.EventType.CREATED,
                TicketEvent.EventType.APPROVED,
                TicketEvent.EventType.APPROVED,
                TicketEvent.EventType.APPROVED,
                TicketEvent.EventType.APPROVED,
                TicketEvent.EventType.DISPATCHED,
                TicketEvent.EventType.ACCEPTED,
                TicketEvent.EventType.COMPLETED,
                TicketEvent.EventType.REVIEW_PASSED,
                TicketEvent.EventType.CLOSED,
                TicketEvent.EventType.SETTLED,
            ],
        )
        # 驳回/返工事件不应出现
        self.assertNotIn(TicketEvent.EventType.REVIEW_REJECTED, types)
        self.assertNotIn(TicketEvent.EventType.REJECTED, types)

    def test_review_rejection_records_event(self):
        """正常链审批通过后派工，片区管理员拒收应记录验收驳回事件。"""
        response = self._submit(self._login(self.reporter), RepairRequest.Urgency.NORMAL)
        repair_id = response.url.rstrip("/").split("/")[-1]
        root = self._login(self.root)
        # 正常链 6 级审批：片区管理员、片区经理、安数部、副总、总经理、董事长
        for _ in range(6):
            root.post(f"/repair/{repair_id}/approve/", {"result": "approved"})
        self.assertEqual(
            RepairRequest.objects.get(pk=repair_id).status,
            RepairRequest.Status.APPROVED,
        )
        root.post("/dispatch/create/", {
            "repair_request": repair_id,
            "engineering_team": self.team.pk,
        })
        dispatch = DispatchOrder.objects.get(repair_request_id=repair_id)
        self.assertEqual(dispatch.status, DispatchOrder.Status.PENDING_ACCEPT)
        root.post(f"/dispatch/{dispatch.pk}/accept/")
        root.post(
            f"/dispatch/{dispatch.pk}/complete/",
            {"media": [SimpleUploadedFile("done.png", PNG_BYTES, content_type="image/png")]},
        )
        reject = root.post(
            f"/dispatch/{dispatch.pk}/review/",
            {"result": "rejected", "note": "工艺不达标，返工"},
        )
        self.assertEqual(reject.status_code, 302)
        event = TicketEvent.objects.filter(
            repair_request_id=repair_id,
            event_type=TicketEvent.EventType.REVIEW_REJECTED,
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.remark, "工艺不达标，返工")
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, DispatchOrder.Status.IN_PROGRESS)
        self.assertTrue(dispatch.is_rejected)

    def test_ticket_event_is_immutable(self):
        response = self._submit(self._login(self.reporter), RepairRequest.Urgency.NORMAL)
        repair_id = response.url.rstrip("/").split("/")[-1]
        event = TicketEvent.objects.get(repair_request_id=repair_id)
        event.detail = "试图篡改"
        with self.assertRaises(ValueError):
            event.save()


# ===================== 备件库存 =====================

class SparePartModelTests(TestCase):
    def test_code_autogenerated_sequentially(self):
        p1 = SparePart.objects.create(name="交流接触器", safety_stock=2)
        p2 = SparePart.objects.create(name="断路器", safety_stock=0)
        prefix = p1.code[:8]
        self.assertTrue(prefix.startswith("SP"))
        self.assertEqual(p1.code, f"{prefix}0001")
        self.assertEqual(p2.code, f"{prefix}0002")

    def test_low_stock_threshold_is_inclusive(self):
        part = SparePart.objects.create(name="保险丝", stock_quantity=5, safety_stock=5)
        self.assertTrue(part.is_low_stock)
        part.stock_quantity = Decimal("5.01")
        part.save(update_fields=["stock_quantity"])
        self.assertFalse(part.is_low_stock)


class StockMovementServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.part = SparePart.objects.create(
            name="继电器", unit="个", stock_quantity=0, safety_stock=2
        )
        # 期初库存通过入库流水建立，保证快照可由台账聚合推导
        apply_stock_movement(cls.part, StockMovement.MovementType.IN, 10)
        cls.region = Region.objects.create(code="ST-R", name="库存片区")
        cls.site = Site.objects.create(code="ST-SITE", name="库存油站", region=cls.region)
        cls.reporter = User.objects.create_user("st_reporter", password="x")
        cls.team = EngineeringTeam.objects.create(
            code="STGC",
            full_name="库存工程队",
            credit_code="ST0000000000000001",
            legal_person="测试法人",
        )
        cls.repair = RepairRequest.objects.create(
            site=cls.site,
            reporter=cls.reporter,
            description="库存测试单",
            status=RepairRequest.Status.ACCEPTING,
        )
        cls.dispatch = DispatchOrder.objects.create(
            repair_request=cls.repair,
            engineering_team=cls.team,
            status=DispatchOrder.Status.IN_PROGRESS,
        )

    def test_in_then_out_updates_snapshot_and_balance(self):
        m1 = apply_stock_movement(self.part, StockMovement.MovementType.IN, "5.50")
        self.part.refresh_from_db()
        self.assertEqual(self.part.stock_quantity, Decimal("15.50"))
        self.assertEqual(m1.balance_after, Decimal("15.50"))

        m2 = apply_stock_movement(self.part, StockMovement.MovementType.OUT, 4)
        self.part.refresh_from_db()
        self.assertEqual(self.part.stock_quantity, Decimal("11.50"))
        self.assertEqual(m2.balance_after, Decimal("11.50"))

        # 库存快照 = 历史流水带符号聚合
        signed = sum(
            (m.quantity if m.movement_type == StockMovement.MovementType.IN else -m.quantity)
            for m in StockMovement.objects.filter(spare_part=self.part)
        )
        self.assertEqual(signed, Decimal("11.50"))

    def test_overdraw_rejected_atomically(self):
        with self.assertRaises(ValueError):
            apply_stock_movement(self.part, StockMovement.MovementType.OUT, "10.01")
        self.part.refresh_from_db()
        self.assertEqual(self.part.stock_quantity, Decimal("10"))
        # 仅保留期初入库一条流水
        self.assertEqual(self.part.movements.count(), 1)

    def test_zero_negative_and_bad_type_rejected(self):
        for qty in (0, "-1", "abc"):
            with self.assertRaises(ValueError):
                apply_stock_movement(self.part, StockMovement.MovementType.IN, qty)
        with self.assertRaises(ValueError):
            apply_stock_movement(self.part, "bogus", 1)
        self.assertEqual(self.part.movements.count(), 1)

    def test_inactive_part_rejects_movement(self):
        self.part.is_active = False
        self.part.save(update_fields=["is_active"])
        with self.assertRaises(ValueError):
            apply_stock_movement(self.part, StockMovement.MovementType.IN, 1)
        with self.assertRaises(ValueError):
            apply_stock_movement(self.part, StockMovement.MovementType.OUT, 1)
        self.assertEqual(self.part.movements.count(), 1)

    def test_only_out_can_link_dispatch(self):
        with self.assertRaises(ValueError):
            apply_stock_movement(
                self.part,
                StockMovement.MovementType.IN,
                1,
                dispatch_order=self.dispatch,
            )
        movement = apply_stock_movement(
            self.part,
            StockMovement.MovementType.OUT,
            1,
            dispatch_order=self.dispatch,
        )
        self.assertEqual(movement.dispatch_order_id, self.dispatch.pk)


@override_settings(ALLOWED_HOSTS=["*"])
class SparePartViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(code="SV-R", name="备件视图片区")
        cls.site = Site.objects.create(code="SV-SITE", name="备件油站", region=cls.region)
        cls.safety = User.objects.create_user("sv_safety", password="x")
        UserProfile.objects.create(user=cls.safety, role=UserProfile.Role.SAFETY_REPAIR)
        cls.leader = User.objects.create_user("sv_leader", password="x")
        UserProfile.objects.create(user=cls.leader, role=UserProfile.Role.TEAM_LEADER)
        cls.team = EngineeringTeam.objects.create(
            code="SVGC",
            full_name="备件视图工程队",
            credit_code="SV0000000000000001",
            legal_person="测试法人",
            bound_user=cls.leader,
        )
        cls.repair = RepairRequest.objects.create(
            site=cls.site,
            reporter=cls.leader,
            description="备件视图测试单",
            status=RepairRequest.Status.ACCEPTING,
        )
        cls.dispatch = DispatchOrder.objects.create(
            repair_request=cls.repair,
            engineering_team=cls.team,
            leader=cls.leader,
            status=DispatchOrder.Status.IN_PROGRESS,
        )

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _part(self, name="按钮", stock=10, safety=2):
        return SparePart.objects.create(
            name=name, unit="个", stock_quantity=stock, safety_stock=safety
        )

    def test_anonymous_redirected_to_login(self):
        response = self.client.get("/spareparts/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_safety_creates_part_with_zero_stock(self):
        response = self._login(self.safety).post("/spareparts/add/", {
            "name": "急停按钮",
            "spec": "XB2-ES542",
            "unit": "个",
            "safety_stock": "3",
            "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        part = SparePart.objects.get(name="急停按钮")
        self.assertTrue(part.code.startswith("SP"))
        self.assertEqual(part.stock_quantity, Decimal("0"))
        self.assertEqual(part.safety_stock, Decimal("3"))

    def test_team_leader_cannot_create_or_move(self):
        part = self._part()
        response = self._login(self.leader).post("/spareparts/add/", {"name": "越权备件"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SparePart.objects.filter(name="越权备件").exists())

        response = self._login(self.leader).post(
            f"/spareparts/{part.pk}/stock-in/", {"quantity": "5"}
        )
        self.assertEqual(response.status_code, 302)
        part.refresh_from_db()
        self.assertEqual(part.stock_quantity, Decimal("10"))
        self.assertEqual(part.movements.count(), 0)

    def test_stock_in_and_overdraw_out_through_view(self):
        part = self._part(stock=5)
        client = self._login(self.safety)
        response = client.post(
            f"/spareparts/{part.pk}/stock-in/",
            {"quantity": "5", "remark": "采购入库"},
        )
        self.assertEqual(response.status_code, 302)
        part.refresh_from_db()
        self.assertEqual(part.stock_quantity, Decimal("10"))

        # 超扣：页面重定向报错，库存与流水不变
        response = client.post(
            f"/spareparts/{part.pk}/stock-out/", {"quantity": "11"}
        )
        self.assertEqual(response.status_code, 302)
        part.refresh_from_db()
        self.assertEqual(part.stock_quantity, Decimal("10"))
        self.assertEqual(part.movements.filter(movement_type=StockMovement.MovementType.OUT).count(), 0)

    def test_out_linked_to_dispatch_records_event(self):
        part = self._part(stock=10, safety=8)
        response = self._login(self.safety).post(
            f"/spareparts/{part.pk}/stock-out/",
            {"quantity": "9", "dispatch_order": self.dispatch.pk, "remark": "维修领用"},
        )
        self.assertEqual(response.status_code, 302)
        part.refresh_from_db()
        self.assertEqual(part.stock_quantity, Decimal("1"))
        movement = part.movements.get(movement_type=StockMovement.MovementType.OUT)
        self.assertEqual(movement.dispatch_order_id, self.dispatch.pk)
        self.assertEqual(movement.operator, self.safety)
        event = self.repair.events.get(event_type=TicketEvent.EventType.STOCK_USED)
        self.assertIn(part.code, event.detail)
        self.assertEqual(event.remark, "维修领用")
        # 派工单详情页展示备件领用记录
        page = self._login(self.safety).get(f"/dispatch/{self.dispatch.pk}/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("备件领用记录", page.content.decode())
        # 跌破安全库存 -> 安数部收到库存预警通知
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.safety,
                category=Notification.Category.STOCK,
                business_code=part.code,
            ).exists()
        )

    def test_low_stock_filter_and_badge(self):
        low = self._part(name="低库存件", stock=1, safety=5)
        normal = self._part(name="充足件", stock=99, safety=1)
        client = self._login(self.safety)

        page = client.get("/spareparts/?low=1")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn(low.code, html)
        self.assertNotIn(normal.code, html)
        self.assertIn("低库存", html)

        # 首页为安数部展示低库存预警卡片
        home = client.get("/")
        self.assertIn("备件库存预警", home.content.decode())

    def test_ledger_visible_on_detail_page(self):
        part = self._part()
        client = self._login(self.safety)
        client.post(f"/spareparts/{part.pk}/stock-in/", {"quantity": "3", "remark": "期初入库"})
        page = client.get(f"/spareparts/{part.pk}/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn("出入库台账", html)
        self.assertIn("期初入库", html)
        self.assertIn("13.00", html)
