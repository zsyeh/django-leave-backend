# views.py
import io, qrcode
from django.utils import timezone
from django.core.paginator import Paginator
from django.contrib.auth.models import User 
from django.shortcuts import get_object_or_404 
from django.conf import settings
from django.http import HttpResponse 

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Leave , Class, StudentProfile
from .serializers import (
    LeaveSerializer,
    UserRegisterSerializer,
    UserProfileSerializer,
    ChangePasswordSerializer,
    StudentCreateSerializer
)
from .decorators import group_required


####### 学生注册
@permission_classes([AllowAny])
class RegisterView(APIView):
    def post(self, request):
        serializer = UserRegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            refresh = RefreshToken.for_user(user)
            return Response({
                'detail': 'User registered successfully!',
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


####### 学生提交请假申请
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def request_leave(request):
    data = request.data.copy()
    data['leave_time'] = timezone.now()
    serializer = LeaveSerializer(data=data, context={'request': request})
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


####### 管理员/教师/mas 查看请假条（分页 + 按 status 过滤）
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch', 'mas')
def AdminLeaveListView(request):
    user = request.user
    status_param = request.query_params.get('status')
    page_num = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 10))

    # 根据角色决定查询范围
    if user.groups.filter(name__in=['admin', 'mas']).exists():
        qs = Leave.objects.all()
    else:
        # tch 组只能看自己学生
        students = user.students.all().values_list('user', flat=True)
        qs = Leave.objects.filter(student__in=students)

    # 按状态过滤
    if status_param is not None:
        qs = qs.filter(status=status_param)

    qs = qs.order_by('-leave_time')

    # 分页
    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page_num)
    serializer = LeaveSerializer(page_obj.object_list, many=True)

    return Response({
        'count': paginator.count,
        'next': page_obj.next_page_number() if page_obj.has_next() else None,
        'previous': page_obj.previous_page_number() if page_obj.has_previous() else None,
        'results': serializer.data,
    })

####### 教师管理员添加学生
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch', 'mas')
def add_student(request):
    serializer = UserProfileSerializer(request.user)#获取导员id
    Isuser = str(serializer.data['last_name'])
    #print(Isuser)
    data = request.data.copy()
    user = request.user
    #print(data['advisor_last_name'])
    # 1. 检查必须字段
    class_name = data.get('class_name')
    if not class_name:
        return Response(
            {"class_name": "此字段为必填。"},
            status=status.HTTP_400_BAD_REQUEST
        )
    #2. 判断角色
    is_admin = user.groups.filter(name__in=['admin', 'mas']).exists()
    if not is_admin:
        usertmp = str(data['advisor_last_name'])#获取请求的导员id
        if not Isuser == usertmp:
                return Response(
                    {"detail": "您只能为自己添加学生。"},
                    status=status.HTTP_403_FORBIDDEN
                )
    # 3. 用 StudentCreateSerializer 来校验并创建
    serializer = StudentCreateSerializer(data=data, context={'request': request})
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    new_user = serializer.save()
    return Response(
        {"message": "学生创建成功。", "username": new_user.username},
        status=status.HTTP_201_CREATED
    )


####### 教师管理员删除学生
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'mas')
def delete_student(request, username):
    """
    安全删除一个学生用户：
    - admin/mas：可删除任意 stu 组用户
    - tch：只能删除自己负责班级的学生
    """
    # 1. 获取目标用户
    try:
        target = User.objects.get(username=username)
    except User.DoesNotExist:
        return Response(
            {"detail": "找不到该学生。"},
            status=status.HTTP_404_NOT_FOUND
        )
    # 2. 确认这是个学生账号
    if not target.groups.filter(name='stu').exists():
        return Response(
            {"detail": "仅能删除学生账号。"},
            status=status.HTTP_400_BAD_REQUEST
        )
    # 3. 权限判断
    requester = request.user
    is_admin = requester.groups.filter(name__in=['admin', 'mas']).exists()
    if not is_admin:
        # 辅导员只能删除自己班级下的学生
        profile = getattr(target, 'studentprofile', None)
        if profile.advisor_id != requester.id:
            return Response(
                {"detail": "您只能删除自己负责班级的学生。"},
                status=status.HTTP_403_FORBIDDEN
            )
    # 4. 执行删除
    target.delete()
    return Response(
        {"message": f"学生 {username} 已被删除。"},
        status=status.HTTP_200_OK
    )


####### 获取学号对应学生信息
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch', 'mas')
def get_student_info(request, username):
    # 1. 找用户
    try:
        target = User.objects.get(username=username)
    except User.DoesNotExist:
        return Response(
            {"detail": "找不到该学生。"},
            status=status.HTTP_404_NOT_FOUND
        )
    # 2. 必须是学生组
    if not target.groups.filter(name='stu').exists():
        return Response(
            {"detail": "仅能查询学生账号。"},
            status=status.HTTP_400_BAD_REQUEST
        )
    # 3. 用现有序列化器拿基础信息
    base_data = UserProfileSerializer(target).data
    # 4. 从 StudentProfile 拿 advisor
    try:
        profile = target.studentprofile
        advisor = profile.advisor     # 关联的 User 对象，可能为 None
    except StudentProfile.DoesNotExist:
        advisor = None
    # 5. 拼入 advisor 信息
    base_data['advisor_id'] = profile.advisor_id if profile else None
    base_data['advisor_name'] = advisor.last_name if advisor else None

    return Response(base_data, status=status.HTTP_200_OK)

####### 修改学生班级导员
@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch', 'mas')
def modify_student_profile(request, username):
    """
    修改学生的 assigned_class 和 advisor：
    - admin/mas：可修改任意学生
    - tch：只能修改自己辅导的学生
    """
    # 1. 找到目标用户
    try:
        target = User.objects.get(username=username)
    except User.DoesNotExist:
        return Response({"detail": "学生不存在。"}, status=status.HTTP_404_NOT_FOUND)

    # 2. 确保这是一个学生账号
    if not target.groups.filter(name='stu').exists():
        return Response({"detail": "目标不是学生账号。"}, status=status.HTTP_400_BAD_REQUEST)

    # 3. 权限校验
    requester = request.user
    is_admin = requester.groups.filter(name__in=['admin', 'mas']).exists()

    if not is_admin:
        # 仅允许修改自己辅导的学生
        profile_qs = StudentProfile.objects.filter(user=target, advisor=requester)
        if not profile_qs.exists():
            return Response(
                {"detail": "您只能修改自己辅导的学生信息。"},
                status=status.HTTP_403_FORBIDDEN
            )
        profile = profile_qs.first()
    else:
        # 管理员直接拿到 profile
        try:
            profile = target.studentprofile
        except StudentProfile.DoesNotExist:
            return Response({"detail": "该学生还没有档案。"}, status=status.HTTP_400_BAD_REQUEST)
    # 4. 读取并校验前端数据
    data = request.data
    new_class_name = data.get('class_name')
    new_advisor_last = data.get('advisor_last_name')
    # 修改班级
    if new_class_name is not None:
        try:
            new_class = Class.objects.get(name=new_class_name)
        except Class.DoesNotExist:
            return Response(
                {"class_name": f"班级 '{new_class_name}' 不存在。"},
                status=status.HTTP_400_BAD_REQUEST
            )
        profile.assigned_class = new_class
    # 修改辅导员
    if new_advisor_last is not None:
        try:
            new_adv = User.objects.get(last_name=new_advisor_last, groups__name='tch')
        except User.DoesNotExist:
            return Response(
                {"advisor_last_name": f"辅导员 '{new_advisor_last}' 不存在或不属于 tch 组。"},
                status=status.HTTP_400_BAD_REQUEST
            )
        profile.advisor = new_adv
    # 至少要有一个字段被修改
    if new_class_name is None and new_advisor_last is None:
        return Response(
            {"detail": "请至少提供 class_name 或 advisor_last_name。"},
            status=status.HTTP_400_BAD_REQUEST
        )
    # 5. 保存并返回
    profile.save()
    return Response({
        "detail": "学生信息更新成功。",
        "username": target.username,
        "class_name": profile.assigned_class.name if profile.assigned_class else None,
        "advisor_last_name": profile.advisor.last_name if profile.advisor else None
    }, status=status.HTTP_200_OK)


####### 学生查询自己请假条（分页 + 按 status 可选）
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@group_required('stu')
def get_student_leaves(request):
    status_param = request.query_params.get('status')
    page_num = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 10))

    qs = Leave.objects.filter(student=request.user)
    if status_param is not None:
        qs = qs.filter(status=status_param)
    qs = qs.order_by('-leave_time')

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page_num)
    serializer = LeaveSerializer(page_obj.object_list, many=True)

    return Response({
        'count': paginator.count,
        'next': page_obj.next_page_number() if page_obj.has_next() else None,
        'previous': page_obj.previous_page_number() if page_obj.has_previous() else None,
        'results': serializer.data,
    })

###### 重置学生密码
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch', 'mas')
def reset_student_password(request, username):
    """
    重置学生密码：
      - admin/mas：可重置任意学生
      - tch：只能重置自己辅导学生的密码
    前端可传 new_password 字段，否则默认 '123456'
    """
    # 1. 找到目标学生
    try:
        student = User.objects.get(username=username)
    except User.DoesNotExist:
        return Response(
            {"detail": "学生不存在。"},
            status=status.HTTP_404_NOT_FOUND
        )
    # 2. 确保是学生账号
    if not student.groups.filter(name='stu').exists():
        return Response(
            {"detail": "目标用户不是学生。"},
            status=status.HTTP_400_BAD_REQUEST
        )
    # 3. 权限校验
    requester = request.user
    is_admin = requester.groups.filter(name__in=['admin', 'mas']).exists()
    if not is_admin:
        # tch 只能重置自己辅导的学生
        if not StudentProfile.objects.filter(user=student, advisor=requester).exists():
            return Response(
                {"detail": "您只能重置自己辅导学生的密码。"},
                status=status.HTTP_403_FORBIDDEN
            )
    # 4. 获取新密码或使用默认
    new_password = request.data.get('new_password', '123456')
    if len(new_password) < 6:
        return Response(
            {"new_password": "密码至少 6 位。"},
            status=status.HTTP_400_BAD_REQUEST
        )
    # 5. 重置并保存
    student.set_password(new_password)
    student.save()

    return Response(
        {"detail": f"学生 {username} 的密码已重置。"},
        status=status.HTTP_200_OK
    )

####### 管理员批准请假
@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch', 'mas')
def approve_leave(request, leave_id):
    try:
        leave = Leave.objects.get(id=leave_id)
    except Leave.DoesNotExist:
        return Response({'error': '假条未找到'}, status=status.HTTP_404_NOT_FOUND)
    leave.status = 1
    leave.approver = request.user.last_name
    leave.save()
    return Response({'status': 'Leave approved'})


####### tch 初审批准请假
@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch')
def pre_approve_leave(request, leave_id):
    try:
        leave = Leave.objects.get(id=leave_id)
    except Leave.DoesNotExist:
        return Response({'error': '假条未找到'}, status=status.HTTP_404_NOT_FOUND)
    leave.status = 5
    leave.approver = request.user.last_name
    leave.save()
    return Response({'status': 'Leave pre-approved'})


####### mas 批准长假期
@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch')
def mas_approve_leave(request, leave_id):
    try:
        leave = Leave.objects.get(id=leave_id)
    except Leave.DoesNotExist:
        return Response({'error': '假条未找到'}, status=status.HTTP_404_NOT_FOUND)
    leave.status = 1
    leave.approver = request.user.last_name
    leave.save()
    return Response({'status': 'Long leave approved'})


####### 管理员拒绝请假
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch', 'mas')
def reject_leave(request, leave_id):
    try:
        leave = Leave.objects.get(id=leave_id)
    except Leave.DoesNotExist:
        return Response({'error': '没有找到该假条'}, status=status.HTTP_404_NOT_FOUND)

    reject_reason = request.data.get('reject_reason')
    if reject_reason:
        leave.reject_reason = reject_reason

    if leave.status in [0, 4]:
        leave.status = 2
        leave.approver = request.user.last_name
        leave.save()
        return Response({'status': '已拒绝该假条'})
    return Response({'error': '只有待批准的假条才能拒绝'}, status=status.HTTP_400_BAD_REQUEST)


####### 管理员/tch 销假
@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
@group_required('admin', 'tch')
def complete_leaving(request, leave_id):
    try:
        leave = Leave.objects.get(id=leave_id)
    except Leave.DoesNotExist:
        return Response({'error': '没有找到假条'}, status=status.HTTP_404_NOT_FOUND)
    if leave.status == 1:
        leave.status = 3
        leave.save()
        return Response({'status': '销假成功'})
    return Response({'error': '只有已批准的假条才能销假'}, status=status.HTTP_400_BAD_REQUEST)


####### 学生取消请假
@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def cancel_leave(request, leave_id):
    try:
        # 只允许删除当前登录用户自己创建的假条
        leave = Leave.objects.get(id=leave_id, student=request.user)
    except Leave.DoesNotExist:
        return Response({'error': '没有找到或没有权限'}, status=status.HTTP_400_BAD_REQUEST)

    if leave.status != 0:
        return Response({'error': '只有待批准的假条才能取消'}, status=status.HTTP_400_BAD_REQUEST)

    leave.delete()
    return Response({'message': '假条已成功取消'}, status=status.HTTP_200_OK)
# def cancel_leave(request, leave_id):
#     try:
#         leave = Leave.objects.get(id=leave_id, student=request.user)
#     except Leave.DoesNotExist:
#         return Response({'error': '没有找到或没有权限'}, status=status.HTTP_404_NOT_FOUND)
#     if leave.status != 0:
#         return Response({'error': '只有待批准的假条才能取消'}, status=status.HTTP_400_BAD_REQUEST)
#     leave.status = -1
#     leave.save()
#     return Response({'message': '假条已取消'}, status=status.HTTP_200_OK)


####### 用户查询自己信息
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def UserInfoView(request):
    serializer = UserProfileSerializer(request.user)
    return Response(serializer.data, status=status.HTTP_200_OK)

####### 防伪二维码
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def leave_qrcode(request, uuid):
    """
    根据 verification_uuid 生成二维码，指向验证页面 URL。
    """
    # 构造被扫描后打开的页面地址
    verify_path = f"/leave/verify/{uuid}/"
    verify_url  = f"https://leave.sdutee.xyz{verify_path}"
    #verify_url = "https://leave.sdutee.xyz"
    # 生成二维码
    img = qrcode.make(verify_url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)

    return HttpResponse(buf, content_type='image/png')

####### 防伪假条查询
@api_view(['GET'])
@permission_classes([AllowAny])
def verify_leave(request, uuid):
    """
    防伪验证：只有已批准(status=1)或已销假(status=3)的假条才返回详情。
    其他状态或不存在，一律返回 400 + “假条不存在或未批准”。
    """
    leave = get_object_or_404(Leave, verification_uuid=uuid)

    # 只允许状态 1 和 3
    if leave.status not in (1, 3):
        return Response(
            {"detail": "假条不存在或未批准"},
            status=status.HTTP_400_BAD_REQUEST
        )

    serializer = LeaveSerializer(leave)
    return Response(serializer.data, status=status.HTTP_200_OK)


####### 修改密码
class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response({"detail": "密码更新成功!"}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    