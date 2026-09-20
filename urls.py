from django.shortcuts import render, redirect
from .models import RepairRequest, Site


def repair_list(request):
    """报修单列表"""
    repairs = RepairRequest.objects.select_related('site', 'reporter').all()
    return render(request, 'approvals/repair_list.html', {'repairs': repairs})


def repair_add(request):
    """新增报修单"""
    if request.method == 'POST':
        # 拿到表单提交的数据
        site_id = request.POST.get('site')
        description = request.POST.get('description')

        # 写入数据库
        RepairRequest.objects.create(
            site_id=site_id,
            reporter=request.user,        # 当前登录用户
            description=description,
        )
        # 提交成功后跳回列表页
        return redirect('repair_list')

    # GET 请求：显示空表单
    sites = Site.objects.all()
    return render(request, 'approvals/repair_add.html', {'sites': sites})