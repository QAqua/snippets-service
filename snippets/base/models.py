import hashlib
import json
import os
import re
import uuid
import xml.sax
from StringIO import StringIO
from collections import namedtuple
from urlparse import urljoin
from xml.sax import ContentHandler

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.urlresolvers import reverse
from django.db import models
from django.template.loader import render_to_string

import jingo
from caching.base import CachingManager, CachingMixin
from jinja2 import Markup
from jinja2.utils import LRUCache

from snippets.base.fields import CountryField, LocaleField, RegexField
from snippets.base.managers import ClientMatchRuleManager, SnippetManager
from snippets.base.storage import OverwriteStorage


CHANNELS = ('release', 'beta', 'aurora', 'nightly')
FIREFOX_STARTPAGE_VERSIONS = ('1', '2', '3', '4')
FENNEC_STARTPAGE_VERSIONS = ('1',)
SNIPPET_WEIGHTS = ((33, 'Appear 1/3rd as often as an average snippet'),
                   (50, 'Appear half as often as an average snippet'),
                   (66, 'Appear 2/3rds as often as an average snippet'),
                   (100, 'Appear as often as an average snippet'),
                   (150, 'Appear 1.5 times as often as an average snippet'),
                   (200, 'Appear twice as often as an average snippet'),
                   (300, 'Appear three times as often as an average snippet'))


def validate_xml(data):
    data_dict = json.loads(data)

    # set up a safer XML parser that does not resolve external
    # entities
    parser = xml.sax.make_parser()
    parser.setContentHandler(ContentHandler())
    parser.setFeature(xml.sax.handler.feature_external_ges, 0)

    for name, value in data_dict.items():
        # Skip over values that aren't strings.
        if not isinstance(value, basestring):
            continue

        value = value.encode('utf-8')
        xml_str = '<div>{0}</div>'.format(value)
        try:
            parser.parse(StringIO(xml_str))
        except xml.sax.SAXParseException as e:
            error_msg = (
                'XML Error in value "{name}": {message} in column {column}'
                .format(name=name, message=e.getMessage(),
                        column=e.getColumnNumber()))
            raise ValidationError(error_msg)
    return data


# NamedTuple that represents a user's client program.
Client = namedtuple('Client', (
    'startpage_version',
    'name',
    'version',
    'appbuildid',
    'build_target',
    'locale',
    'channel',
    'os_version',
    'distribution',
    'distribution_version'
))


# Cache for compiled snippet templates. Using jinja's built in cache
# requires either an extra trip to the database/cache or jumping through
# hoops.
template_cache = LRUCache(100)


class SnippetBundle(object):
    """
    Group of snippets to be sent to a particular client configuration.
    """
    def __init__(self, client):
        self.client = client
        self.storage = OverwriteStorage()

        self._snippets = None

    @property
    def key(self):
        """A unique key for this bundle as a sha1 hexdigest."""
        # Key should consist of snippets that are in the bundle plus any
        # properties of the client that may change the snippet code
        # being sent.
        key_properties = [snippet.id for snippet in self.snippets]
        key_properties.extend([
            self.client.startpage_version,
            self.client.locale,
        ])

        key_string = u'_'.join(unicode(prop) for prop in key_properties)
        return hashlib.sha1(key_string).hexdigest()

    @property
    def cache_key(self):
        return u'bundle_' + self.key

    @property
    def expired(self):
        """
        If True, the code for this bundle should be re-generated before
        use.
        """
        return not cache.get(self.cache_key)

    @property
    def filename(self):
        return u'bundles/{0}.html'.format(self.key)

    @property
    def url(self):
        return self.storage.url(self.filename)

    @property
    def snippets(self):
        # Lazy-load snippets on first access.
        if self._snippets is None:
            self._snippets = (Snippet.cached_objects
                              .filter(disabled=False)
                              .match_client(self.client)
                              .order_by('priority')
                              .select_related('template')
                              .filter_by_available())
        return self._snippets

    def generate(self):
        """Generate and save the code for this snippet bundle."""
        bundle_content = render_to_string('base/fetch_snippets.html', {
            'snippets': self.snippets,
            'client': self.client,
            'locale': self.client.locale,
        })

        self.storage.save(self.filename, ContentFile(bundle_content))
        cache.set(self.cache_key, True, settings.SNIPPET_BUNDLE_TIMEOUT)


class SnippetTemplate(CachingMixin, models.Model):
    """
    A template for the body of a snippet. Can have multiple variables that the
    snippet will fill in.
    """
    name = models.CharField(max_length=255, unique=True)
    code = models.TextField()

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    objects = models.Manager()
    cached_objects = CachingManager()

    def render(self, ctx):
        ctx.setdefault('snippet_id', 0)

        # Check if template is in cache, and cache it if it's not.
        cache_key = hashlib.sha1(self.code).hexdigest()
        template = template_cache.get(cache_key)
        if not template:
            template = jingo.env.from_string(self.code)
            template_cache[cache_key] = template
        return template.render(ctx)

    def __unicode__(self):
        return self.name


class SnippetTemplateVariable(CachingMixin, models.Model):
    """
    A variable for a template that an individual snippet can fill in with its
    own content.
    """
    TEXT = 0
    IMAGE = 1
    SMALLTEXT = 2
    CHECKBOX = 3
    TYPE_CHOICES = ((TEXT, 'Text'), (IMAGE, 'Image'), (SMALLTEXT, 'Small Text'),
                    (CHECKBOX, 'Checkbox'))

    template = models.ForeignKey(SnippetTemplate, related_name='variable_set')
    name = models.CharField(max_length=255)
    type = models.IntegerField(choices=TYPE_CHOICES, default=TEXT)
    description = models.TextField(blank=True, default='')

    objects = models.Manager()
    cached_objects = CachingManager()

    def __unicode__(self):
        return u'{0}: {1}'.format(self.template.name, self.name)


class ClientMatchRule(CachingMixin, models.Model):
    """Defines a rule that matches a snippet to certain clients."""
    description = models.CharField(max_length=255, unique=True)
    is_exclusion = models.BooleanField(default=False)

    startpage_version = RegexField()
    name = RegexField()
    version = RegexField()
    appbuildid = RegexField()
    build_target = RegexField()
    locale = RegexField()
    channel = RegexField()
    os_version = RegexField()
    distribution = RegexField()
    distribution_version = RegexField()

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    objects = models.Manager()
    cached_objects = ClientMatchRuleManager()

    class Meta:
        ordering = ('-modified',)

    def matches(self, client):
        """Evaluate whether this rule matches the given client."""
        match = True
        for field in client._fields:
            field_value = getattr(self, field, None)
            if not field_value:
                continue

            client_field_value = getattr(client, field)
            if field_value.startswith('/'):  # Match field as a regex.
                if re.match(field_value[1:-1], client_field_value) is None:
                    match = False
                    break
            elif field_value != client_field_value:  # Match field as a string.
                match = False
                break

        # Exclusion rules match clients that do not match their rule.
        return not match if self.is_exclusion else match

    def __unicode__(self):
        return self.description


class Snippet(CachingMixin, models.Model):
    name = models.CharField(max_length=255, unique=True)
    template = models.ForeignKey(SnippetTemplate)
    data = models.TextField(default='{}', validators=[validate_xml])

    priority = models.IntegerField(default=0, blank=True)
    disabled = models.BooleanField(default=True)

    country = CountryField('Geolocation Country', blank=True, default='')

    publish_start = models.DateTimeField(blank=True, null=True)
    publish_end = models.DateTimeField(blank=True, null=True)

    on_release = models.BooleanField(default=True, verbose_name='Release')
    on_beta = models.BooleanField(default=False, verbose_name='Beta')
    on_aurora = models.BooleanField(default=False, verbose_name='Aurora')
    on_nightly = models.BooleanField(default=False, verbose_name='Nightly')

    on_startpage_1 = models.BooleanField(default=False, verbose_name='Version 1')
    on_startpage_2 = models.BooleanField(default=True, verbose_name='Version 2')
    on_startpage_3 = models.BooleanField(default=True, verbose_name='Version 3')
    on_startpage_4 = models.BooleanField(default=True, verbose_name='Version 4')

    weight = models.IntegerField(
        'Prevalence', choices=SNIPPET_WEIGHTS, default=100,
        help_text='How often should this snippet be shown to users?')

    client_match_rules = models.ManyToManyField(
        ClientMatchRule, blank=True, verbose_name='Client Match Rules')

    exclude_from_search_providers = models.ManyToManyField(
        'SearchProvider', blank=True, verbose_name='Excluded Search Providers')

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    objects = models.Manager()
    cached_objects = SnippetManager()

    class Meta:
        ordering = ('-modified',)

    def render(self):
        data = json.loads(self.data)
        snippet_id = self.id or 0
        data.setdefault('snippet_id', snippet_id)

        # Add snippet ID to template variables.
        for key, value in data.items():
            if isinstance(value, basestring):
                data[key] = value.replace(u'<snippet_id>', unicode(snippet_id))

        # Use a list for attrs to make the output order predictable.
        attrs = [('data-snippet-id', self.id),
                 ('data-weight', self.weight),
                 ('class', 'snippet-metadata')]
        if self.country:
            attrs.append(('data-country', self.country))

        if self.id:
            # Avoid using values_list() because django-cache-machine
            # does not support it.
            search_engine_identifiers = [
                provider.identifier for provider in self.exclude_from_search_providers.all()
            ]
            if search_engine_identifiers:
                attrs.append(('data-exclude-from-search-engines', ','.join(search_engine_identifiers)))

        attr_string = ' '.join('{0}="{1}"'.format(key, value) for key, value in
                               attrs)

        rendered_snippet = u'<div {attrs}>{content}</div>'.format(
            attrs=attr_string,
            content=self.template.render(data)
        )

        return Markup(rendered_snippet)

    @property
    def channels(self):
        channels = []
        for channel in CHANNELS:
            if getattr(self, 'on_{0}'.format(channel), False):
                channels.append(channel)
        return channels

    def __unicode__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('base.show', kwargs={'snippet_id': self.id})


class SnippetLocale(CachingMixin, models.Model):
    snippet = models.ForeignKey(Snippet, related_name='locale_set')
    locale = LocaleField()

    objects = models.Manager()
    cached_objects = CachingManager()


class JSONSnippet(CachingMixin, models.Model):
    name = models.CharField(max_length=255, unique=True)
    priority = models.IntegerField(default=0, blank=True)
    disabled = models.BooleanField(default=True)

    icon = models.TextField(help_text='Icon should be a 96x96px PNG.')
    text = models.CharField(max_length=140,
                            help_text='Maximum length 140 characters.')
    url = models.CharField(max_length=500)

    country = CountryField('Geolocation Country', blank=True, default='')

    publish_start = models.DateTimeField(blank=True, null=True)
    publish_end = models.DateTimeField(blank=True, null=True)

    on_release = models.BooleanField(default=True, verbose_name='Release')
    on_beta = models.BooleanField(default=False, verbose_name='Beta')
    on_aurora = models.BooleanField(default=False, verbose_name='Aurora')
    on_nightly = models.BooleanField(default=False, verbose_name='Nightly')

    on_startpage_1 = models.BooleanField(default=True, verbose_name='Version 1')

    weight = models.IntegerField(
        'Prevalence', choices=SNIPPET_WEIGHTS, default=100,
        help_text='How often should this snippet be shown to users?')

    client_match_rules = models.ManyToManyField(
        ClientMatchRule, blank=True, verbose_name='Client Match Rules')

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    objects = models.Manager()
    cached_objects = SnippetManager()

    class Meta:
        ordering = ('-modified',)
        verbose_name = 'JSON Snippet'

    def __unicode__(self):
        return self.name


class JSONSnippetLocale(CachingMixin, models.Model):
    snippet = models.ForeignKey(JSONSnippet, related_name='locale_set')
    locale = LocaleField()

    objects = models.Manager()
    cached_objects = CachingManager()


class UploadedFile(models.Model):
    FILES_ROOT = 'files'  # Directory name inside MEDIA_ROOT

    def _generate_filename(instance, filename):
        """Generate a new unique filename while preserving the original
        filename extension. If an existing UploadedFile gets updated
        do not generate a new filename.
        """

        # Instance is new UploadedFile, generate a filename
        if not instance.id:
            ext = os.path.splitext(filename)[1]
            filename = str(uuid.uuid4()) + ext
            return os.path.join(UploadedFile.FILES_ROOT, filename)

        # Use existing filename.
        obj = UploadedFile.objects.get(id=instance.id)
        return obj.file.name

    file = models.FileField(storage=OverwriteStorage(), upload_to=_generate_filename)
    name = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    def __unicode__(self):
        return self.name

    @property
    def url(self):
        site_url = getattr(settings, 'CDN_URL', settings.SITE_URL)
        full_url = urljoin(site_url, self.file.url)
        return full_url

    @property
    def snippets(self):
        return Snippet.objects.filter(
            models.Q(data__contains=self.file.url) |
            models.Q(template__code__contains=self.file.url)
        )


class SearchProvider(CachingMixin, models.Model):
    name = models.CharField(max_length=255, unique=True)
    identifier = models.CharField(max_length=255)

    objects = CachingManager()

    def __unicode__(self):
        return self.name

    class Meta:
        ordering = ('id',)
