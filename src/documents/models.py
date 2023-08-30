import datetime
import logging
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Final
from typing import Optional

import dateutil.parser
import pathvalidate
from celery import states
from django.conf import settings
from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from documents.parsers import get_default_file_extension

ALL_STATES = sorted(states.ALL_STATES)
TASK_STATE_CHOICES = sorted(zip(ALL_STATES, ALL_STATES))


class ModelWithOwner(models.Model):
    owner = models.ForeignKey(
        User,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        verbose_name=_("owner"),
    )

    class Meta:
        abstract = True


class MatchingModel(ModelWithOwner):
    MATCH_NONE = 0
    MATCH_ANY = 1
    MATCH_ALL = 2
    MATCH_LITERAL = 3
    MATCH_REGEX = 4
    MATCH_FUZZY = 5
    MATCH_AUTO = 6

    MATCHING_ALGORITHMS = (
        (MATCH_NONE, _("None")),
        (MATCH_ANY, _("Any word")),
        (MATCH_ALL, _("All words")),
        (MATCH_LITERAL, _("Exact match")),
        (MATCH_REGEX, _("Regular expression")),
        (MATCH_FUZZY, _("Fuzzy word")),
        (MATCH_AUTO, _("Automatic")),
    )

    name = models.CharField(_("name"), max_length=128)

    match = models.CharField(_("match"), max_length=256, blank=True)

    matching_algorithm = models.PositiveIntegerField(
        _("matching algorithm"),
        choices=MATCHING_ALGORITHMS,
        default=MATCH_ANY,
    )

    is_insensitive = models.BooleanField(_("is insensitive"), default=True)

    class Meta:
        abstract = True
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["name", "owner"],
                name="%(app_label)s_%(class)s_unique_name_owner",
            ),
            models.UniqueConstraint(
                name="%(app_label)s_%(class)s_name_uniq",
                fields=["name"],
                condition=models.Q(owner__isnull=True),
            ),
        ]

    def __str__(self):
        return self.name


class Correspondent(MatchingModel):
    class Meta(MatchingModel.Meta):
        verbose_name = _("correspondent")
        verbose_name_plural = _("correspondents")


class Tag(MatchingModel):
    color = models.CharField(_("color"), max_length=7, default="#a6cee3")

    is_inbox_tag = models.BooleanField(
        _("is inbox tag"),
        default=False,
        help_text=_(
            "Marks this tag as an inbox tag: All newly consumed "
            "documents will be tagged with inbox tags.",
        ),
    )

    class Meta(MatchingModel.Meta):
        verbose_name = _("tag")
        verbose_name_plural = _("tags")


class DocumentType(MatchingModel):
    class Meta(MatchingModel.Meta):
        verbose_name = _("document type")
        verbose_name_plural = _("document types")


class StoragePath(MatchingModel):
    path = models.CharField(
        _("path"),
        max_length=512,
    )

    class Meta(MatchingModel.Meta):
        verbose_name = _("storage path")
        verbose_name_plural = _("storage paths")


class Document(ModelWithOwner):
    STORAGE_TYPE_UNENCRYPTED = "unencrypted"
    STORAGE_TYPE_GPG = "gpg"
    STORAGE_TYPES = (
        (STORAGE_TYPE_UNENCRYPTED, _("Unencrypted")),
        (STORAGE_TYPE_GPG, _("Encrypted with GNU Privacy Guard")),
    )

    correspondent = models.ForeignKey(
        Correspondent,
        blank=True,
        null=True,
        related_name="documents",
        on_delete=models.SET_NULL,
        verbose_name=_("correspondent"),
    )

    storage_path = models.ForeignKey(
        StoragePath,
        blank=True,
        null=True,
        related_name="documents",
        on_delete=models.SET_NULL,
        verbose_name=_("storage path"),
    )

    title = models.CharField(_("title"), max_length=128, blank=True, db_index=True)

    document_type = models.ForeignKey(
        DocumentType,
        blank=True,
        null=True,
        related_name="documents",
        on_delete=models.SET_NULL,
        verbose_name=_("document type"),
    )

    content = models.TextField(
        _("content"),
        blank=True,
        help_text=_(
            "The raw, text-only data of the document. This field is "
            "primarily used for searching.",
        ),
    )

    mime_type = models.CharField(_("mime type"), max_length=256, editable=False)

    tags = models.ManyToManyField(
        Tag,
        related_name="documents",
        blank=True,
        verbose_name=_("tags"),
    )

    checksum = models.CharField(
        _("checksum"),
        max_length=32,
        editable=False,
        unique=True,
        help_text=_("The checksum of the original document."),
    )

    archive_checksum = models.CharField(
        _("archive checksum"),
        max_length=32,
        editable=False,
        blank=True,
        null=True,
        help_text=_("The checksum of the archived document."),
    )

    created = models.DateTimeField(_("created"), default=timezone.now, db_index=True)

    modified = models.DateTimeField(
        _("modified"),
        auto_now=True,
        editable=False,
        db_index=True,
    )

    storage_type = models.CharField(
        _("storage type"),
        max_length=11,
        choices=STORAGE_TYPES,
        default=STORAGE_TYPE_UNENCRYPTED,
        editable=False,
    )

    added = models.DateTimeField(
        _("added"),
        default=timezone.now,
        editable=False,
        db_index=True,
    )

    filename = models.FilePathField(
        _("filename"),
        max_length=1024,
        editable=False,
        default=None,
        unique=True,
        null=True,
        help_text=_("Current filename in storage"),
    )

    archive_filename = models.FilePathField(
        _("archive filename"),
        max_length=1024,
        editable=False,
        default=None,
        unique=True,
        null=True,
        help_text=_("Current archive filename in storage"),
    )

    original_filename = models.CharField(
        _("original filename"),
        max_length=1024,
        editable=False,
        default=None,
        unique=False,
        null=True,
        help_text=_("The original name of the file when it was uploaded"),
    )

    ARCHIVE_SERIAL_NUMBER_MIN: Final[int] = 0
    ARCHIVE_SERIAL_NUMBER_MAX: Final[int] = 0xFF_FF_FF_FF

    archive_serial_number = models.PositiveIntegerField(
        _("archive serial number"),
        blank=True,
        null=True,
        unique=True,
        db_index=True,
        validators=[
            MaxValueValidator(ARCHIVE_SERIAL_NUMBER_MAX),
            MinValueValidator(ARCHIVE_SERIAL_NUMBER_MIN),
        ],
        help_text=_(
            "The position of this document in your physical document archive.",
        ),
    )

    class Meta:
        ordering = ("-created",)
        verbose_name = _("document")
        verbose_name_plural = _("documents")

    def __str__(self) -> str:
        # Convert UTC database time to local time
        created = datetime.date.isoformat(timezone.localdate(self.created))

        res = f"{created}"

        if self.correspondent:
            res += f" {self.correspondent}"
        if self.title:
            res += f" {self.title}"
        return res

    @property
    def source_path(self) -> Path:
        if self.filename:
            fname = str(self.filename)
        else:
            fname = f"{self.pk:07}{self.file_type}"
            if self.storage_type == self.STORAGE_TYPE_GPG:
                fname += ".gpg"  # pragma: no cover

        return (settings.ORIGINALS_DIR / Path(fname)).resolve()

    @property
    def source_file(self):
        return open(self.source_path, "rb")

    @property
    def has_archive_version(self) -> bool:
        return self.archive_filename is not None

    @property
    def archive_path(self) -> Optional[Path]:
        if self.has_archive_version:
            return (settings.ARCHIVE_DIR / Path(str(self.archive_filename))).resolve()
        else:
            return None

    @property
    def archive_file(self):
        return open(self.archive_path, "rb")

    def get_public_filename(self, archive=False, counter=0, suffix=None) -> str:
        """
        Returns a sanitized filename for the document, not including any paths.
        """
        result = str(self)

        if counter:
            result += f"_{counter:02}"

        if suffix:
            result += suffix

        if archive:
            result += ".pdf"
        else:
            result += self.file_type

        return pathvalidate.sanitize_filename(result, replacement_text="-")

    @property
    def file_type(self):
        return get_default_file_extension(self.mime_type)

    @property
    def thumbnail_path(self) -> Path:
        webp_file_name = f"{self.pk:07}.webp"
        if self.storage_type == self.STORAGE_TYPE_GPG:
            webp_file_name += ".gpg"

        webp_file_path = settings.THUMBNAIL_DIR / Path(webp_file_name)

        return webp_file_path.resolve()

    @property
    def thumbnail_file(self):
        return open(self.thumbnail_path, "rb")

    @property
    def created_date(self):
        return timezone.localdate(self.created)


class Log(models.Model):
    LEVELS = (
        (logging.DEBUG, _("debug")),
        (logging.INFO, _("information")),
        (logging.WARNING, _("warning")),
        (logging.ERROR, _("error")),
        (logging.CRITICAL, _("critical")),
    )

    group = models.UUIDField(_("group"), blank=True, null=True)

    message = models.TextField(_("message"))

    level = models.PositiveIntegerField(
        _("level"),
        choices=LEVELS,
        default=logging.INFO,
    )

    created = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta:
        ordering = ("-created",)
        verbose_name = _("log")
        verbose_name_plural = _("logs")

    def __str__(self):
        return self.message


class SavedView(ModelWithOwner):
    class Meta:
        ordering = ("name",)
        verbose_name = _("saved view")
        verbose_name_plural = _("saved views")

    name = models.CharField(_("name"), max_length=128)

    show_on_dashboard = models.BooleanField(
        _("show on dashboard"),
    )
    show_in_sidebar = models.BooleanField(
        _("show in sidebar"),
    )

    sort_field = models.CharField(
        _("sort field"),
        max_length=128,
        null=True,
        blank=True,
    )
    sort_reverse = models.BooleanField(_("sort reverse"), default=False)


class SavedViewFilterRule(models.Model):
    RULE_TYPES = [
        (0, _("title contains")),
        (1, _("content contains")),
        (2, _("ASN is")),
        (3, _("correspondent is")),
        (4, _("document type is")),
        (5, _("is in inbox")),
        (6, _("has tag")),
        (7, _("has any tag")),
        (8, _("created before")),
        (9, _("created after")),
        (10, _("created year is")),
        (11, _("created month is")),
        (12, _("created day is")),
        (13, _("added before")),
        (14, _("added after")),
        (15, _("modified before")),
        (16, _("modified after")),
        (17, _("does not have tag")),
        (18, _("does not have ASN")),
        (19, _("title or content contains")),
        (20, _("fulltext query")),
        (21, _("more like this")),
        (22, _("has tags in")),
        (23, _("ASN greater than")),
        (24, _("ASN less than")),
        (25, _("storage path is")),
        (26, _("has correspondent in")),
        (27, _("does not have correspondent in")),
        (28, _("has document type in")),
        (29, _("does not have document type in")),
        (30, _("has storage path in")),
        (31, _("does not have storage path in")),
        (32, _("owner is")),
        (33, _("has owner in")),
        (34, _("does not have owner")),
        (35, _("does not have owner in")),
    ]

    saved_view = models.ForeignKey(
        SavedView,
        on_delete=models.CASCADE,
        related_name="filter_rules",
        verbose_name=_("saved view"),
    )

    rule_type = models.PositiveIntegerField(_("rule type"), choices=RULE_TYPES)

    value = models.CharField(_("value"), max_length=255, blank=True, null=True)

    class Meta:
        verbose_name = _("filter rule")
        verbose_name_plural = _("filter rules")

    def __str__(self) -> str:
        return f"SavedViewFilterRule: {self.rule_type} : {self.value}"


class ConfigurationOptionManager(models.Manager):
    CONFIGURATION_KEYS = {
        # "REDIS": {"type": str, "default": "redis://localhost:6379"},
        # "REDIS_PREFIX": {"type": str, "default": ""},
        # "DBENGINE": {"type": str, "default": "sqlite"},
        # "DBHOST": {"type": str, "default": None},
        # "DBPORT": {"type": int, "default": 5432},
        # "DBNAME": {"type": str, "default": "paperless"},
        # "DBUSER": {"type": str, "default": "paperless"},
        # "DBPASS": {"type": str, "default": "paperless"},
        # "DBSSLMODE": {"type": str, "default": "prefer"},
        # "DBSSLROOTCERT": {"type": str, "default": None},
        # "DBSSLCERT": {"type": str, "default": None},
        # "DBSSLKEY": {"type": str, "default": None},
        # "DB_TIMEOUT": {"type": int, "default": None},
        # "TIKA_ENABLED": {"type": bool, "default": False},
        # "TIKA_ENDPOINT": {"type": str, "default": "http://localhost:9998"},
        # "TIKA_GOTENBERG_ENDPOINT": {"type": str, "default": "http://localhost:3000"},
        # "CONSUMPTION_DIR": {"type": str, "default": "../consume/"},
        # "DATA_DIR": {"type": str, "default": "../data/"},
        # "TRASH_DIR": {"type": str, "default": "../media/trash"},
        # "MEDIA_ROOT": {"type": str, "default": "../media/"},
        # "STATICDIR": {"type": str, "default": "../static/"},
        # "FILENAME_FORMAT": {"type": str, "default": None},
        # "FILENAME_FORMAT_REMOVE_NONE": {"type": bool, "default": False},
        # "LOGGING_DIR": {"type": str, "default": "PAPERLESS_DATA_DIR/log/"},
        # "NLTK_DIR": {"type": str, "default": "/usr/share/nltk_data"},
        # "LOGROTATE_MAX_SIZE": {"type": int, "default": 1024 * 1024},
        # "LOGROTATE_MAX_BACKUPS": {"type": int, "default": 20},
        # "SECRET_KEY": {
        #     "type": str,
        #     "default": "e11fl1oa-*ytql8p)(06fbj4ukrlo+n7k&q5+$1md7i+mge=ee",
        # },
        # "URL": {"type": str, "default": ""},
        # "CSRF_TRUSTED_ORIGINS": {"type": str, "default": ""},
        # "ALLOWED_HOSTS": {"type": str, "default": "*"},
        # "CORS_ALLOWED_HOSTS": {"type": str, "default": "http://localhost:8000"},
        # "TRUSTED_PROXIES": {"type": str, "default": ""},
        # "FORCE_SCRIPT_NAME": {"type": str, "default": None},
        # "STATIC_URL": {"type": str, "default": "/static/"},
        # "AUTO_LOGIN_USERNAME": {"type": str, "default": None},
        # "ADMIN_USER": {"type": str, "default": None},
        # "ADMIN_PASSWORD": {"type": str, "default": None},
        # "ADMIN_MAIL": {"type": str, "default": "root@localhost"},
        # "COOKIE_PREFIX": {"type": str, "default": ""},
        # "ENABLE_HTTP_REMOTE_USER": {"type": str, "default": False},
        # "HTTP_REMOTE_USER_HEADER_NAME": {"type": str, "default": "HTTP_REMOTE_USER"},
        # "LOGOUT_REDIRECT_URL": {"type": str, "default": None},
        # "USE_X_FORWARD_HOST": {"type": bool, "default": False},
        # "USE_X_FORWARD_PORT": {"type": bool, "default": False},
        # "PROXY_SSL_HEADER": {"type": str, "default": None},
        # "EMAIL_CERTIFICATE_FILE": {"type": str, "default": None},
        "OCR_LANGUAGE": {"type": str, "default": "eng"},
        "OCR_MODE": {"type": str, "default": "skip"},
        "OCR_SKIP_ARCHIVE_FILE": {"type": str, "default": "never"},
        "OCR_CLEAN": {"type": str, "default": "clean"},
        "OCR_DESKEW": {"type": bool, "default": True},
        "OCR_ROTATE_PAGES": {"type": bool, "default": True},
        "OCR_ROTATE_PAGES_THRESHOLD": {"type": bool, "default": True},
        "OCR_OUTPUT_TYPE": {"type": str, "default": "pdfa"},
        "OCR_PAGES": {"type": int, "default": 0},
        "OCR_IMAGE_DPI": {"type": int, "default": None},
        "OCR_MAX_IMAGE_PIXELS": {"type": int, "default": None},
        "OCR_USER_ARGS": {"type": str, "default": None},
        # "TASK_WORKERS": {"type": int, "default": 1},
        # "THREADS_PER_WORKER": {"type": int, "default": None},
        # "WORKER_TIMEOUT": {"type": int, "default": 1800},
        # "TIME_ZONE": {"type": str, "default": "UTC"},
        # "ENABLE_NLTK": {"type": bool, "default": True},
        # "EMAIL_TASK_CRON": {"type": str, "default": "*/10 * * * *"},
        # "TRAIN_TASK_CRON": {"type": str, "default": "5 */1 * * *"},
        # "INDEX_TASK_CRON": {"type": str, "default": "0 0 * * *"},
        # "SANITY_TASK_CRON": {"type": str, "default": "30 0 * * sun"},
        # "ENABLE_COMPRESSION": {"type": bool, "default": True},
        # "CONVERT_MEMORY_LIMIT": {"type": int, "default": 0},
        # "CONVERT_TMPDIR": {"type": str, "default": None},
        # "CONSUMER_DELETE_DUPLICATES": {"type": bool, "default": False},
        # "CONSUMER_RECURSIVE": {"type": bool, "default": False},
        # "CONSUMER_SUBDIRS_AS_TAGS": {"type": bool, "default": False},
        # "CONSUMER_IGNORE_PATTERNS": {
        #     "type": str,
        #     "default": '[".DS_Store", ".DS_STORE", "._*", ".stfolder/*", ".stversions/*", ".localized/*", "desktop.ini", "@eaDir/*"]',  # noqa: E501
        # },
        # "CONSUMER_BARCODE_SCANNER": {"type": str, "default": "PYZBAR"},
        # "PRE_CONSUME_SCRIPT": {"type": str, "default": None},
        # "POST_CONSUME_SCRIPT": {"type": str, "default": None},
        # "FILENAME_DATE_ORDER": {"type": str, "default": None},
        "NUMBER_OF_SUGGESTED_DATES": {"type": int, "default": 3},
        # "THUMBNAIL_FONT_NAME": {
        #     "type": str,
        #     "default": "/usr/share/fonts/liberation/LiberationSerif-Regular.ttf",
        # },
        "IGNORE_DATES": {"type": str, "default": ""},
        "DATE_ORDER": {"type": str, "default": "DMY"},
        # "CONSUMER_POLLING": {"type": int, "default": 0},
        # "CONSUMER_POLLING_RETRY_COUNT": {"type": int, "default": 5},
        # "CONSUMER_POLLING_DELAY": {"type": int, "default": 5},
        # "CONSUMER_INOTIFY_DELAY": {"type": float, "default": 0.5},
        # "CONSUMER_ENABLE_BARCODES": {"type": bool, "default": False},
        # "CONSUMER_BARCODE_TIFF_SUPPORT": {"type": bool, "default": False},
        # "CONSUMER_BARCODE_STRING": {"type": str, "default": "PATCHT"},
        # "CONSUMER_ENABLE_ASN_BARCODE": {"type": bool, "default": False},
        # "CONSUMER_ASN_BARCODE_PREFIX": {"type": str, "default": "ASN"},
        # "CONSUMER_BARCODE_UPSCALE": {"type": float, "default": 0.0},
        # "CONSUMER_BARCODE_DPI": {"type": int, "default": 300},
        # "CONSUMER_ENABLE_COLLATE_DOUBLE_SIDED": {"type": bool, "default": False},
        # "CONSUMER_COLLATE_DOUBLE_SIDED_SUBDIR_NAME": {
        #     "type": str,
        #     "default": "double-sided",
        # },
        # "CONSUMER_COLLATE_DOUBLE_SIDED_TIFF_SUPPORT": {"type": bool, "default": False}, # noqa: E501
        # "CONVERT_BINARY": {"type": str, "default": "convert"},
        # "GS_BINARY": {"type": str, "default": "gs"},
    }

    def __getattr__(self, item):
        if item not in self.CONFIGURATION_KEYS:
            raise AttributeError

        query = self.filter(key=item)
        if query.exists() and query.first().value:
            return self.CONFIGURATION_KEYS[item]["type"](query.first().value)
        else:
            return self._get_default(item)

    def set_config(self, key, value):
        if key in self.CONFIGURATION_KEYS:
            if not type(value) == self.CONFIGURATION_KEYS[key]["type"]:
                raise TypeError

            self.filter(key=key).update(value=value)
        else:
            raise KeyError

    def _get_default(self, key):
        if f"PAPERLESS_{key}" in os.environ:
            return os.environ[f"PAPERLESS_{key}"]
        elif "default" in self.CONFIGURATION_KEYS[key]:
            return self.CONFIGURATION_KEYS[key]["default"]
        else:
            return None


class ConfigurationOption(models.Model):
    key = models.CharField(max_length=128, unique=True)
    value = models.CharField(blank=True, max_length=1024, null=True)

    objects = ConfigurationOptionManager()

    def __str__(self):
        return self.key


db_settings = ConfigurationOption.objects


# TODO: why is this in the models file?
# TODO: how about, what is this and where is it documented?
# It appears to parsing JSON from an environment variable to get a title and date from
# the filename, if possible, as a higher priority than either document filename or
# content parsing
class FileInfo:
    REGEXES = OrderedDict(
        [
            (
                "created-title",
                re.compile(
                    r"^(?P<created>\d{8}(\d{6})?Z) - (?P<title>.*)$",
                    flags=re.IGNORECASE,
                ),
            ),
            ("title", re.compile(r"(?P<title>.*)$", flags=re.IGNORECASE)),
        ],
    )

    def __init__(
        self,
        created=None,
        correspondent=None,
        title=None,
        tags=(),
        extension=None,
    ):
        self.created = created
        self.title = title
        self.extension = extension
        self.correspondent = correspondent
        self.tags = tags

    @classmethod
    def _get_created(cls, created):
        try:
            return dateutil.parser.parse(f"{created[:-1]:0<14}Z")
        except ValueError:
            return None

    @classmethod
    def _get_title(cls, title):
        return title

    @classmethod
    def _mangle_property(cls, properties, name):
        if name in properties:
            properties[name] = getattr(cls, f"_get_{name}")(properties[name])

    @classmethod
    def from_filename(cls, filename) -> "FileInfo":
        # Mutate filename in-place before parsing its components
        # by applying at most one of the configured transformations.
        for pattern, repl in settings.FILENAME_PARSE_TRANSFORMS:
            (filename, count) = pattern.subn(repl, filename)
            if count:
                break

        # do this after the transforms so that the transforms can do whatever
        # with the file extension.
        filename_no_ext = os.path.splitext(filename)[0]

        if filename_no_ext == filename and filename.startswith("."):
            # This is a very special case where there is no text before the
            # file type.
            # TODO: this should be handled better. The ext is not removed
            #  because usually, files like '.pdf' are just hidden files
            #  with the name pdf, but in our case, its more likely that
            #  there's just no name to begin with.
            filename = ""
            # This isn't too bad either, since we'll just not match anything
            # and return an empty title. TODO: actually, this is kinda bad.
        else:
            filename = filename_no_ext

        # Parse filename components.
        for regex in cls.REGEXES.values():
            m = regex.match(filename)
            if m:
                properties = m.groupdict()
                cls._mangle_property(properties, "created")
                cls._mangle_property(properties, "title")
                return cls(**properties)


# Extending User Model Using a One-To-One Link
class UiSettings(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="ui_settings",
    )
    settings = models.JSONField(null=True)

    def __str__(self):
        return self.user.username


class PaperlessTask(models.Model):
    task_id = models.CharField(
        max_length=255,
        unique=True,
        verbose_name=_("Task ID"),
        help_text=_("Celery ID for the Task that was run"),
    )

    acknowledged = models.BooleanField(
        default=False,
        verbose_name=_("Acknowledged"),
        help_text=_("If the task is acknowledged via the frontend or API"),
    )

    task_file_name = models.CharField(
        null=True,
        max_length=255,
        verbose_name=_("Task Filename"),
        help_text=_("Name of the file which the Task was run for"),
    )

    task_name = models.CharField(
        null=True,
        max_length=255,
        verbose_name=_("Task Name"),
        help_text=_("Name of the Task which was run"),
    )

    status = models.CharField(
        max_length=30,
        default=states.PENDING,
        choices=TASK_STATE_CHOICES,
        verbose_name=_("Task State"),
        help_text=_("Current state of the task being run"),
    )
    date_created = models.DateTimeField(
        null=True,
        default=timezone.now,
        verbose_name=_("Created DateTime"),
        help_text=_("Datetime field when the task result was created in UTC"),
    )
    date_started = models.DateTimeField(
        null=True,
        default=None,
        verbose_name=_("Started DateTime"),
        help_text=_("Datetime field when the task was started in UTC"),
    )
    date_done = models.DateTimeField(
        null=True,
        default=None,
        verbose_name=_("Completed DateTime"),
        help_text=_("Datetime field when the task was completed in UTC"),
    )
    result = models.TextField(
        null=True,
        default=None,
        verbose_name=_("Result Data"),
        help_text=_(
            "The data returned by the task",
        ),
    )

    def __str__(self) -> str:
        return f"Task {self.task_id}"


class Note(models.Model):
    note = models.TextField(
        _("content"),
        blank=True,
        help_text=_("Note for the document"),
    )

    created = models.DateTimeField(
        _("created"),
        default=timezone.now,
        db_index=True,
    )

    document = models.ForeignKey(
        Document,
        blank=True,
        null=True,
        related_name="notes",
        on_delete=models.CASCADE,
        verbose_name=_("document"),
    )

    user = models.ForeignKey(
        User,
        blank=True,
        null=True,
        related_name="notes",
        on_delete=models.SET_NULL,
        verbose_name=_("user"),
    )

    class Meta:
        ordering = ("created",)
        verbose_name = _("note")
        verbose_name_plural = _("notes")

    def __str__(self):
        return self.note
