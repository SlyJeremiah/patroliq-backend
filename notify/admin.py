from django.contrib import admin

from .models import NotificationLog


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "channel", "to", "title", "backend", "success")
    list_filter = ("channel", "success", "backend")
    readonly_fields = [f.name for f in NotificationLog._meta.fields]
