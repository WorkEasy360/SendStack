from django.contrib import admin

from .models import AssignmentPolicy, CannedReply, TeamMember, WorkingHours


@admin.register(TeamMember)
class TeamMemberAdmin(admin.ModelAdmin):
    list_display = ["user", "tenant", "role", "is_available", "auto_assign", "max_open_conversations"]
    list_filter = ["tenant", "role", "is_available"]


@admin.register(AssignmentPolicy)
class AssignmentPolicyAdmin(admin.ModelAdmin):
    list_display = ["tenant", "mode", "first_response_target_minutes", "working_hours_enabled", "send_away_message"]


@admin.register(WorkingHours)
class WorkingHoursAdmin(admin.ModelAdmin):
    list_display = ["tenant", "weekday", "is_open", "opens_at", "closes_at"]
    list_filter = ["tenant", "weekday"]


@admin.register(CannedReply)
class CannedReplyAdmin(admin.ModelAdmin):
    list_display = ["shortcut", "title", "tenant", "usage_count"]
    search_fields = ["shortcut", "title", "body"]
