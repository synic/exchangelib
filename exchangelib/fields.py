from __future__ import unicode_literals

import abc
import base64
from decimal import Decimal
import logging

from six import string_types

from .errors import ErrorInvalidServerVersion
from .ewsdatetime import EWSDateTime, EWSDate, NaiveDateTimeNotAllowed
from .services import TNS
from .util import create_element, get_xml_attr, get_xml_attrs, set_xml_value, value_to_xml_text, is_iterable
from .version import Build

string_type = string_types[0]
log = logging.getLogger(__name__)


def split_field_path(field_path):
    """Return the individual parts of a field path that may, apart from the fieldname, have label and subfield parts.
    Examples:
        'start' -> ('start', None, None)
        'phone_numbers__PrimaryPhone' -> ('phone_numbers', 'PrimaryPhone', None)
        'physical_addresses__Home__street' -> ('physical_addresses', 'Home', 'street')
    """
    if not isinstance(field_path, string_types):
        raise ValueError("Field path '%s' must be a string" % field_path)
    search_parts = field_path.split('__')
    field = search_parts[0]
    try:
        label = search_parts[1]
    except IndexError:
        label = None
    try:
        subfield = search_parts[2]
    except IndexError:
        subfield = None
    return field, label, subfield


def resolve_field_path(field_path, folder, strict=True):
    # Takes the name of a field, or '__'-delimited path to a subfield, and returns the corresponding Field object,
    # label and SubField object
    from .indexed_properties import SingleFieldIndexedElement, MultiFieldIndexedElement
    fieldname, label, subfieldname = split_field_path(field_path)
    field = folder.get_item_field_by_fieldname(fieldname)
    subfield = None
    if isinstance(field, IndexedField):
        if strict and not label:
            raise ValueError(
                "IndexedField path '%s' must specify label, e.g. '%s__%s'"
                % (field_path, fieldname, field.value_cls.LABEL_FIELD.default)
            )
        valid_labels = field.value_cls.LABEL_FIELD.supported_choices(version=folder.account.version)
        if label and label not in valid_labels:
            raise ValueError(
                "Label '%s' on IndexedField path '%s' must be one of %s"
                % (label, field_path, ', '.join(valid_labels))
            )
        if issubclass(field.value_cls, MultiFieldIndexedElement):
            if strict and not subfieldname:
                raise ValueError(
                    "IndexedField path '%s' must specify subfield, e.g. '%s__%s__%s'"
                    % (field_path, fieldname, label, field.value_cls.FIELDS[0].name)
                )

            if subfieldname:
                try:
                    subfield = field.value_cls.get_field_by_fieldname(subfieldname)
                except ValueError:
                    fnames = ', '.join(f.name for f in field.value_cls.supported_fields(version=folder.account.version))
                    raise ValueError(
                        "Subfield '%s' on IndexedField path '%s' must be one of %s"
                        % (subfieldname, field_path, fnames)
                    )
        else:
            assert issubclass(field.value_cls, SingleFieldIndexedElement)
            if subfieldname:
                raise ValueError(
                    "IndexedField path '%s' must not specify subfield, e.g. just '%s__%s'"
                    % (field_path, fieldname, label)
                )
            subfield = field.value_cls.value_field(version=folder.account.version)
    else:
        if label or subfieldname:
            raise ValueError(
                "Field path '%s' must not specify label or subfield, e.g. just '%s'"
                % (field_path, fieldname)
            )
    return field, label, subfield


class FieldPath(object):
    """ Holds values needed to point to a single field. For indexed properties, we allow setting eiterh field,
    field and label, or field, label and subfield. This allows pointing to either the full indexed property set, a
    property with a specific label, or a particular subfield field on that property. """
    def __init__(self, field, label=None, subfield=None):
        # 'label' and 'subfield' are only used for IndexedField fields
        assert isinstance(field, (FieldURIField, ExtendedPropertyField))
        if label:
            assert isinstance(label, string_types)
        if subfield:
            assert isinstance(subfield, SubField)
        self.field = field
        self.label = label
        self.subfield = subfield

    @classmethod
    def from_string(cls, s, folder, strict=False):
        field, label, subfield = resolve_field_path(s, folder=folder, strict=strict)
        return cls(field=field, label=label, subfield=subfield)

    def get_value(self, item):
        # For indexed properties, get either the full property set, the property with matching label, or a particular
        # subfield.
        if self.label:
            for subitem in getattr(item, self.field.name):
                if subitem.label == self.label:
                    if self.subfield:
                        return getattr(subitem, self.subfield.name)
                    return subitem
            return None  # No item with this label
        return getattr(item, self.field.name)

    def to_xml(self):
        if isinstance(self.field, IndexedField):
            if not self.label or not self.subfield:
                raise ValueError("Field path for indexed field '%s' is missing label and/or subfield" % self.field.name)
            return self.subfield.field_uri_xml(field_uri=self.field.field_uri, label=self.label)
        else:
            return self.field.field_uri_xml()

    def expand(self, version):
        # If this path does not point to a specific subfield on an indexed property, return all the possible path
        # combinations for this field path.
        if isinstance(self.field, IndexedField):
            labels = [self.label] if self.label else self.field.value_cls.LABEL_FIELD.supported_choices(version=version)
            subfields = [self.subfield] if self.subfield else self.field.value_cls.supported_fields(version=version)
            for label in labels:
                for subfield in subfields:
                    yield FieldPath(field=self.field, label=label, subfield=subfield)
        else:
            yield self

    @property
    def path(self):
        if self.label:
            from .indexed_properties import SingleFieldIndexedElement
            if issubclass(self.field.value_cls, SingleFieldIndexedElement) or not self.subfield:
                return '%s__%s' % (self.field.name, self.label)
            return '%s__%s__%s' % (self.field.name, self.label, self.subfield.name)
        return self.field.name

    def __eq__(self, other):
        return hash(self) == hash(other)

    def __hash__(self):
        return hash((self.field, self.label, self.subfield))


class FieldOrder(object):
    """ Holds values needed to call server-side sorting on a single field path """
    def __init__(self, field_path, reverse=False):
        self.field_path = field_path
        self.reverse = reverse

    @classmethod
    def from_string(cls, s, folder):
        field_path = FieldPath.from_string(s.lstrip('-'), folder=folder, strict=True)
        reverse = s.startswith('-')
        return cls(field_path=field_path, reverse=reverse)

    def to_xml(self):
        field_order = create_element('t:FieldOrder', Order='Descending' if self.reverse else 'Ascending')
        field_order.append(self.field_path.to_xml())
        return field_order


class Field(object):
    """
    Holds information related to an item field
    """
    __metaclass__ = abc.ABCMeta
    value_cls = None
    is_list = False
    # Is the field a complex EWS type? Quoting the EWS FindItem docs:
    #
    #   The FindItem operation returns only the first 512 bytes of any streamable property. For Unicode, it returns
    #   the first 255 characters by using a null-terminated Unicode string. It does not return any of the message
    #   body formats or the recipient lists.
    #
    is_complex = False

    def __init__(self, name, is_required=False, is_required_after_save=False, is_read_only=False,
                 is_read_only_after_send=False, is_searchable=True, default=None, supported_from=None):
        self.name = name
        self.default = default  # Default value if none is given
        self.is_required = is_required
        # Some fields cannot be deleted on update. Default to True if 'is_required' is set
        self.is_required_after_save = is_required or is_required_after_save
        self.is_read_only = is_read_only
        # Set this for fields that raise ErrorInvalidPropertyUpdateSentMessage on update after send. Default to True
        # if 'is_read_only' is set
        self.is_read_only_after_send = is_read_only or is_read_only_after_send
        # Define whether the field can be used in a QuerySet. For some reason, EWS disallows searching on some fields,
        # instead throwing ErrorInvalidValueForProperty
        self.is_searchable = is_searchable
        # The Exchange build when this field was introduced. When talking with versions prior to this version,
        # we will ignore this field.
        if supported_from is not None:
            assert isinstance(supported_from, Build)
        self.supported_from = supported_from

    def clean(self, value, version=None):
        if not self.supports_version(version):
            raise ErrorInvalidServerVersion("Field '%s' does not support EWS builds prior to %s (server has %s)" % (
                self.name, self.supported_from, version))
        if value is None:
            if self.is_required and self.default is None:
                raise ValueError("'%s' is a required field with no default" % self.name)
            return self.default
        if self.is_list:
            if not is_iterable(value):
                raise ValueError("Field '%s' value '%s' must be a list" % (self.name, value))
            for v in value:
                if not isinstance(v, self.value_cls):
                    raise TypeError('Field %s value "%s" must be of type %s' % (self.name, v, self.value_cls))
                if hasattr(v, 'clean'):
                    v.clean(version=version)
        else:
            if not isinstance(value, self.value_cls):
                raise TypeError("Field '%s' value '%s' must be of type %s" % (self.name, value, self.value_cls))
            if hasattr(value, 'clean'):
                value.clean(version=version)
        return value

    @abc.abstractmethod
    def from_xml(self, elem, account):
        raise NotImplementedError()

    @abc.abstractmethod
    def to_xml(self, value, version):
        raise NotImplementedError()

    def supports_version(self, version):
        # 'version' is a Version instance, for convenience by callers
        if not self.supported_from or not version:
            return True
        return version.build >= self.supported_from

    def __eq__(self, other):
        return hash(self) == hash(other)

    @abc.abstractmethod
    def __hash__(self):
        raise NotImplementedError()

    def __repr__(self):
        return self.__class__.__name__ + '(%s)' % ', '.join('%s=%r' % (f, getattr(self, f)) for f in (
            'name', 'value_cls', 'is_list', 'is_complex', 'default'))


class FieldURIField(Field):
    def __init__(self, *args, **kwargs):
        self.field_uri = kwargs.pop('field_uri', None)
        super(FieldURIField, self).__init__(*args, **kwargs)
        # See all valid FieldURI values at https://msdn.microsoft.com/en-us/library/office/aa494315(v=exchg.150).aspx
        # The field_uri has a prefix when the FieldURI points to an Item field.
        if self.field_uri is None:
            self.field_uri_postfix = None
        elif ':' in self.field_uri:
            self.field_uri_postfix = self.field_uri.split(':')[1]
        else:
            self.field_uri_postfix = self.field_uri

    def to_xml(self, value, version):
        field_elem = create_element(self.request_tag())
        return set_xml_value(field_elem, value, version=version)

    def field_uri_xml(self):
        assert self.field_uri
        return create_element('t:FieldURI', FieldURI=self.field_uri)

    def request_tag(self):
        assert self.field_uri_postfix
        return 't:%s' % self.field_uri_postfix

    def response_tag(self):
        assert self.field_uri_postfix
        return '{%s}%s' % (TNS, self.field_uri_postfix)

    def __hash__(self):
        return hash(self.field_uri)


class BooleanField(FieldURIField):
    value_cls = bool

    def from_xml(self, elem, account):
        field_elem = elem.find(self.response_tag())
        val = None if field_elem is None else field_elem.text or None
        if val is not None:
            try:
                return {
                    'true': True,
                    'false': False,
                }[val]
            except KeyError:
                log.warning("Cannot convert value '%s' on field '%s' to type %s", val, self.name, self.value_cls)
                return None
        return self.default


class IntegerField(FieldURIField):
    value_cls = int

    def __init__(self, *args, **kwargs):
        self.min = kwargs.pop('min', None)
        self.max = kwargs.pop('max', None)
        super(IntegerField, self).__init__(*args, **kwargs)

    def clean(self, value, version=None):
        value = super(IntegerField, self).clean(value, version=version)
        if value is not None:
            if self.is_list:
                for v in value:
                    if self.min is not None and v < self.min:
                        raise ValueError(
                            "value '%s' on field '%s' must be greater than %s" % (value, self.name, self.min))
                    if self.max is not None and v > self.max:
                        raise ValueError("value '%s' on field '%s' must be less than %s" % (value, self.name, self.max))
            else:
                if self.min is not None and value < self.min:
                    raise ValueError("value '%s' on field '%s' must be greater than %s" % (value, self.name, self.min))
                if self.max is not None and value > self.max:
                    raise ValueError("value '%s' on field '%s' must be less than %s" % (value, self.name, self.max))
        return value

    def from_xml(self, elem, account):
        field_elem = elem.find(self.response_tag())
        val = None if field_elem is None else field_elem.text or None
        if val is not None:
            try:
                return self.value_cls(val)
            except ValueError:
                log.warning("Cannot convert value '%s' on field '%s' to type %s", val, self.name, self.value_cls)
                return None
        return self.default


class DecimalField(IntegerField):
    value_cls = Decimal


class EnumField(IntegerField):
    # A field type where you can enter either the 1-based index in an enum (tuple), or the enum value. Values will be
    # stored internally as integers.
    def __init__(self, *args, **kwargs):
        self.enum = kwargs.pop('enum')
        super(EnumField, self).__init__(*args, **kwargs)
        self.min = 1
        self.max = len(self.enum)

    def clean(self, value, version=None):
        if self.is_list:
            value = list(value)  # Convert to something we can index
            for i, v in enumerate(value):
                if isinstance(v, string_types):
                    if v not in self.enum:
                        raise ValueError(
                            "List value '%s' on field '%s' must be one of %s" % (v, self.name, self.enum))
                    value[i] = self.enum.index(v) + 1
            if not len(value):
                raise ValueError("Value '%s' on field '%s' must not be empty" % (value, self.name))
            if len(value) > len(set(value)):
                raise ValueError("List entries '%s' on field '%s' must be unique" % (value, self.name))
        else:
            if isinstance(value, string_types):
                if value not in self.enum:
                    raise ValueError(
                        "Value '%s' on field '%s' must be one of %s" % (value, self.name, self.enum))
                value = self.enum.index(value) + 1
        return super(EnumField, self).clean(value, version=version)

    def from_xml(self, elem, account):
        field_elem = elem.find(self.response_tag())
        val = None if field_elem is None else field_elem.text or None
        if val is not None:
            try:
                if self.is_list:
                    return [self.enum.index(v) + 1 for v in val.split(' ')]
                else:
                    return self.enum.index(val) + 1
            except ValueError:
                log.warning("Cannot convert value '%s' on field '%s' to type %s", val, self.name, self.value_cls)
                return None
        return self.default

    def to_xml(self, value, version):
        field_elem = create_element(self.request_tag())
        if self.is_list:
            return set_xml_value(field_elem, ' '.join(self.enum[v - 1] for v in sorted(value)), version=version)
        else:
            return set_xml_value(field_elem, self.enum[value - 1], version=version)


class EffectiveRightsField(FieldURIField):
    RIGHTS = ('CreateAssociated', 'CreateContents', 'CreateHierarchy',
              'Delete', 'Modify', 'Read', 'ViewPrivateItems')

    def from_xml(self, elem, account):
        field_elem = elem.find(self.response_tag())
        effective_rights = []

        for right in self.RIGHTS:
            value = get_xml_attr(
                field_elem, '{%s}%s' % (TNS, right))
            if value == 'true':
                effective_rights.append(right)

        return effective_rights

    def to_xml(self, value, version):
        import ipdb; ipdb.set_trace()


class EnumListField(EnumField):
    is_list = True


class Base64Field(FieldURIField):
    value_cls = bytes
    is_complex = True

    def from_xml(self, elem, account):
        field_elem = elem.find(self.response_tag())
        val = None if field_elem is None else field_elem.text or None
        if val is not None:
            return base64.b64decode(val)
        return self.default

    def to_xml(self, value, version):
        field_elem = create_element(self.request_tag())
        return set_xml_value(field_elem, base64.b64encode(value).decode('ascii'), version=version)


class DateField(FieldURIField):
    value_cls = EWSDate

    def from_xml(self, elem, account):
        field_elem = elem.find(self.response_tag())
        val = None if field_elem is None else field_elem.text or None
        if val is not None:
            try:
                return self.value_cls.from_string(val)
            except ValueError:
                log.warning("Cannot convert value '%s' on field '%s' to type %s", val, self.name, self.value_cls)
                return None
        return self.default


class DateTimeField(FieldURIField):
    value_cls = EWSDateTime

    def clean(self, value, version=None):
        if value is not None and isinstance(value, self.value_cls) and not value.tzinfo:
            raise ValueError("Field '%s' must be timezone aware" % self.name)
        return super(DateTimeField, self).clean(value, version=version)

    def from_xml(self, elem, account):
        field_elem = elem.find(self.response_tag())
        val = None if field_elem is None else field_elem.text or None
        if val is not None:
            try:
                return self.value_cls.from_string(val)
            except ValueError as e:
                if isinstance(e, NaiveDateTimeNotAllowed):
                    # We encountered a naive datetime. Convert to timezone-aware datetime using the default timezone of
                    # the account.
                    local_dt = e.args[0]
                    log.info('Encountered naive datetime %s on field %s. Assuming timezone %s', local_dt, self.name,
                             account.default_timezone)
                    return account.default_timezone.localize(self.value_cls.from_datetime(local_dt))
                log.warning("Cannot convert value '%s' on field '%s' to type %s", val, self.name, self.value_cls)
                return None
        return self.default


class TextField(FieldURIField):
    value_cls = string_type

    def __init__(self, *args, **kwargs):
        self.max_length = kwargs.pop('max_length', 255)  # Fields supporting longer messages are complex fields
        super(TextField, self).__init__(*args, **kwargs)

    def clean(self, value, version=None):
        value = super(TextField, self).clean(value, version=version)
        if self.max_length and value is not None:
            if self.is_list:
                for v in value:
                    if len(v) > self.max_length:
                        raise ValueError("'%s' value '%s' exceeds length %s" % (self.name, v, self.max_length))
            else:
                if len(value) > self.max_length:
                    raise ValueError("'%s' value '%s' exceeds length %s" % (self.name, value, self.max_length))
        return value

    def from_xml(self, elem, account):
        field_elem = elem.find(self.response_tag())
        val = None if field_elem is None else field_elem.text or None
        if val is not None:
            return val
        return self.default


class TextListField(TextField):
    is_list = True

    def from_xml(self, elem, account):
        iter_elem = elem.find(self.response_tag())
        if iter_elem is not None:
            return get_xml_attrs(iter_elem, '{%s}String' % TNS)
        return self.default


class URIField(TextField):
    # Helper to mark strings that must conform to xsd:anyURI
    # If we want an URI validator, see http://stackoverflow.com/questions/14466585/is-this-regex-correct-for-xsdanyuri
    pass


class EmailField(TextField):
    # A helper class used for email address string that we can use for email validation
    pass


class Choice(object):
    """ Implements versioned choices for the ChoiceField field"""
    def __init__(self, value, supported_from=None):
        self.value = value
        self.supported_from = supported_from

    def supports_version(self, version):
        # 'version' is a Version instance, for convenience by callers
        if not self.supported_from or not version:
            return True
        return version.build >= self.supported_from


class ChoiceField(TextField):
    def __init__(self, *args, **kwargs):
        self.choices = kwargs.pop('choices')
        super(ChoiceField, self).__init__(*args, **kwargs)

    def clean(self, value, version=None):
        value = super(ChoiceField, self).clean(value, version=version)
        if value is None:
            return None
        for c in self.choices:
            if c.value != value:
                continue
            if not c.supports_version(version):
                raise ErrorInvalidServerVersion("Choice '%s' does not support EWS builds prior to %s (server has %s)"
                                                % (self.name, self.supported_from, version))
            return value
        raise ValueError("Invalid choice '%s' for field '%s'. Valid choices are: %s" % (
            value, self.name, ', '.join(self.supported_choices(version=version))))

    def supported_choices(self, version=None):
        return {c.value for c in self.choices if c.supports_version(version)}


class BodyField(TextField):
    is_complex = True

    def __init__(self, *args, **kwargs):
        from .properties import Body
        self.value_cls = Body
        super(BodyField, self).__init__(*args, **kwargs)
        self.max_length = None

    def clean(self, value, version=None):
        if value is not None and not isinstance(value, self.value_cls):
            value = self.value_cls(value)
        return super(BodyField, self).clean(value, version=version)

    def from_xml(self, elem, account):
        from .properties import Body, HTMLBody
        field_elem = elem.find(self.response_tag())
        val = None if field_elem is None else field_elem.text or None
        if val is not None:
            body_type = field_elem.get('BodyType')
            return {
                Body.body_type: Body,
                HTMLBody.body_type: HTMLBody,
            }[body_type](val)
        return self.default

    def to_xml(self, value, version):
        from .properties import Body, HTMLBody
        field_elem = create_element(self.request_tag())
        body_type = {
            Body: Body.body_type,
            HTMLBody: HTMLBody.body_type,
        }[type(value)]
        field_elem.set('BodyType', body_type)
        return set_xml_value(field_elem, value, version=version)


class EWSElementField(FieldURIField):
    def __init__(self, *args, **kwargs):
        self.value_cls = kwargs.pop('value_cls')
        super(EWSElementField, self).__init__(*args, **kwargs)

    def from_xml(self, elem, account):
        if self.is_list:
            iter_elem = elem.find(self.response_tag())
            if iter_elem is not None:
                return [self.value_cls.from_xml(elem=e, account=account)
                        for e in iter_elem.findall(self.value_cls.response_tag())]
        else:
            if self.field_uri is None:
                sub_elem = elem.find(self.value_cls.response_tag())
            else:
                sub_elem = elem.find(self.response_tag())
            if sub_elem is not None:
                return self.value_cls.from_xml(elem=sub_elem, account=account)
        return self.default

    def to_xml(self, value, version):
        if self.field_uri is None:
            return value.to_xml(version=version)
        field_elem = create_element(self.request_tag())
        return set_xml_value(field_elem, value, version=version)


class EWSElementListField(EWSElementField):
    is_list = True
    is_complex = True


class RecurrenceField(EWSElementField):
    is_complex = True

    def __init__(self, *args, **kwargs):
        from .recurrence import Recurrence
        kwargs['value_cls'] = Recurrence
        super(RecurrenceField, self).__init__(*args, **kwargs)

    def to_xml(self, value, version):
        return value.to_xml(version=version)


class OccurrenceField(EWSElementField):
    is_complex = True


class OccurrenceListField(OccurrenceField):
    is_list = True


class MessageHeaderField(EWSElementListField):
    is_complex = True

    def __init__(self, *args, **kwargs):
        from .properties import MessageHeader
        kwargs['value_cls'] = MessageHeader
        super(MessageHeaderField, self).__init__(*args, **kwargs)


class MailboxField(EWSElementField):
    is_complex = True  # FindItem only returns the name, not the email address

    def __init__(self, *args, **kwargs):
        from .properties import Mailbox
        kwargs['value_cls'] = Mailbox
        super(MailboxField, self).__init__(*args, **kwargs)

    def clean(self, value, version=None):
        if isinstance(value, string_types):
            value = self.value_cls(email_address=value)
        return super(MailboxField, self).clean(value, version=version)

    def from_xml(self, elem, account):
        if self.field_uri is None:
            sub_elem = elem.find(self.value_cls.response_tag())
        else:
            sub_elem = elem.find(self.response_tag())
        if sub_elem is not None:
            if self.field_uri is not None:
                # We want the nested Mailbox, not the wrapper element
                return self.value_cls.from_xml(elem=sub_elem.find(self.value_cls.response_tag()), account=account)
            else:
                return self.value_cls.from_xml(elem=sub_elem, account=account)
        return self.default


class MailboxListField(EWSElementListField):
    is_complex = True

    def __init__(self, *args, **kwargs):
        from .properties import Mailbox
        kwargs['value_cls'] = Mailbox
        super(MailboxListField, self).__init__(*args, **kwargs)

    def clean(self, value, version=None):
        if value is not None:
            value = [self.value_cls(email_address=s) if isinstance(s, string_types) else s for s in value]
        return super(MailboxListField, self).clean(value, version=version)


class MemberListField(EWSElementListField):
    is_complex = True

    def __init__(self, *args, **kwargs):
        from .properties import Member
        kwargs['value_cls'] = Member
        super(MemberListField, self).__init__(*args, **kwargs)

    def clean(self, value, version=None):
        if value is not None:
            from .properties import Mailbox
            value = [
                self.value_cls(mailbox=Mailbox(email_address=s)) if isinstance(s, string_types) else s for s in value
            ]
        return super(MemberListField, self).clean(value, version=version)


class AttendeesField(EWSElementListField):
    is_complex = True

    def __init__(self, *args, **kwargs):
        from .properties import Attendee
        kwargs['value_cls'] = Attendee
        super(AttendeesField, self).__init__(*args, **kwargs)

    def clean(self, value, version=None):
        from .properties import Mailbox
        if value is not None:
            value = [self.value_cls(mailbox=Mailbox(email_address=s), response_type='Accept')
                     if isinstance(s, string_types) else s for s in value]
        return super(AttendeesField, self).clean(value, version=version)


class AttachmentField(EWSElementListField):
    is_complex = True

    def __init__(self, *args, **kwargs):
        from .attachments import Attachment
        kwargs['value_cls'] = Attachment
        super(AttachmentField, self).__init__(*args, **kwargs)

    def from_xml(self, elem, account):
        from .attachments import FileAttachment, ItemAttachment
        iter_elem = elem.find(self.response_tag())
        # Look for both FileAttachment and ItemAttachment
        if iter_elem is not None:
            attachments = []
            for att_type in (FileAttachment, ItemAttachment):
                attachments.extend(
                    [att_type.from_xml(elem=e, account=account) for e in iter_elem.findall(att_type.response_tag())]
                )
            return attachments
        return self.default


class LabelField(ChoiceField):
    # A field to hold the label on an IndexedElement
    def from_xml(self, elem, account):
        return elem.get(self.field_uri)


class SubField(Field):
    # A field to hold the value on an SingleFieldIndexedElement
    value_cls = string_type

    def from_xml(self, elem, account):
        return elem.text

    def to_xml(self, value, version):
        return value

    def field_uri_xml(self, field_uri, label):
        return create_element('t:IndexedFieldURI', FieldURI=field_uri, FieldIndex=label)

    def __hash__(self):
        return hash(self.name)


class EmailSubField(SubField):
    # A field to hold the value on an SingleFieldIndexedElement
    value_cls = string_type

    def from_xml(self, elem, account):
        return elem.text or elem.get('Name')  # Sometimes elem.text is empty. Exchange saves the same in 'Name' attr


class NamedSubField(SubField):
    # A field to hold the value on an MultiFieldIndexedElement
    value_cls = string_type

    def __init__(self, *args, **kwargs):
        self.field_uri = kwargs.pop('field_uri')
        assert ':' not in self.field_uri
        super(NamedSubField, self).__init__(*args, **kwargs)

    def from_xml(self, elem, account):
        field_elem = elem.find(self.response_tag())
        val = None if field_elem is None else field_elem.text or None
        if val is not None:
            return val
        return self.default

    def field_uri_xml(self, field_uri, label):
        return create_element('t:IndexedFieldURI', FieldURI='%s:%s' % (field_uri, self.field_uri), FieldIndex=label)

    def request_tag(self):
        return 't:%s' % self.field_uri

    def response_tag(self):
        return '{%s}%s' % (TNS, self.field_uri)


class IndexedField(FieldURIField):
    PARENT_ELEMENT_NAME = None

    def __init__(self, *args, **kwargs):
        self.value_cls = kwargs.pop('value_cls')
        super(IndexedField, self).__init__(*args, **kwargs)

    def from_xml(self, elem, account):
        if self.is_list:
            iter_elem = elem.find(self.response_tag())
            if iter_elem is not None:
                return [self.value_cls.from_xml(elem=e, account=account)
                        for e in iter_elem.findall(self.value_cls.response_tag())]
        else:
            sub_elem = elem.find(self.response_tag())
            if sub_elem is not None:
                return self.value_cls.from_xml(elem=sub_elem, account=account)
        return self.default

    def to_xml(self, value, version):
        return set_xml_value(create_element('t:%s' % self.PARENT_ELEMENT_NAME), value, version)

    def field_uri_xml(self):
        # Callers must call field_uri_xml() on the subfield
        raise NotImplementedError()

    @classmethod
    def response_tag(cls):
        return '{%s}%s' % (TNS, cls.PARENT_ELEMENT_NAME)

    def __hash__(self):
        return hash(self.field_uri)


class EmailAddressField(IndexedField):
    is_list = True

    PARENT_ELEMENT_NAME = 'EmailAddresses'

    def __init__(self, *args, **kwargs):
        from .indexed_properties import EmailAddress
        kwargs['value_cls'] = EmailAddress
        super(EmailAddressField, self).__init__(*args, **kwargs)


class PhoneNumberField(IndexedField):
    is_list = True

    PARENT_ELEMENT_NAME = 'PhoneNumbers'

    def __init__(self, *args, **kwargs):
        from .indexed_properties import PhoneNumber
        kwargs['value_cls'] = PhoneNumber
        super(PhoneNumberField, self).__init__(*args, **kwargs)


class PhysicalAddressField(IndexedField):
    # MSDN: https://msdn.microsoft.com/en-us/library/office/aa564323(v=exchg.150).aspx
    is_list = True

    PARENT_ELEMENT_NAME = 'PhysicalAddresses'

    def __init__(self, *args, **kwargs):
        from .indexed_properties import PhysicalAddress
        kwargs['value_cls'] = PhysicalAddress
        super(PhysicalAddressField, self).__init__(*args, **kwargs)


class ExtendedPropertyField(Field):
    def __init__(self, *args, **kwargs):
        self.value_cls = kwargs.pop('value_cls')
        super(ExtendedPropertyField, self).__init__(*args, **kwargs)

    def clean(self, value, version=None):
        if value is None:
            if self.is_required:
                raise ValueError("'%s' is a required field" % self.name)
            return self.default
        elif not isinstance(value, self.value_cls):
            # Allow keeping ExtendedProperty field values as their simple Python type, but run clean() anyway
            tmp = self.value_cls(value)
            tmp.clean(version=version)
            return value
        value.clean(version=version)
        return value

    def field_uri_xml(self):
        elem = create_element('t:ExtendedFieldURI')
        cls = self.value_cls
        if cls.distinguished_property_set_id:
            elem.set('DistinguishedPropertySetId', cls.distinguished_property_set_id)
        if cls.property_set_id:
            elem.set('PropertySetId', cls.property_set_id)
        if cls.property_tag:
            elem.set('PropertyTag', cls.property_tag_as_hex())
        if cls.property_name:
            elem.set('PropertyName', cls.property_name)
        if cls.property_id:
            elem.set('PropertyId', value_to_xml_text(cls.property_id))
        elem.set('PropertyType', cls.property_type)
        return elem

    def from_xml(self, elem, account):
        extended_properties = elem.findall(self.value_cls.response_tag())
        for extended_property in extended_properties:
            extended_field_uri = extended_property.find('{%s}ExtendedFieldURI' % TNS)
            match = True
            for k, v in self.value_cls.properties_map().items():
                if extended_field_uri.get(k) != v:
                    match = False
                    break
            if match:
                return self.value_cls.from_xml(elem=extended_property, account=account)
        return self.default

    def to_xml(self, value, version):
        extended_property = create_element(self.value_cls.request_tag())
        set_xml_value(extended_property, self.field_uri_xml(), version=version)
        if isinstance(value, self.value_cls):
            set_xml_value(extended_property, value, version=version)
        else:
            # Allow keeping ExtendedProperty field values as their simple Python type
            set_xml_value(extended_property, self.value_cls(value), version=version)
        return extended_property

    def __hash__(self):
        return hash(self.name)


class ItemField(FieldURIField):
    def __init__(self, *args, **kwargs):
        super(ItemField, self).__init__(*args, **kwargs)

    @property
    def value_cls(self):
        # This is a workaround for circular imports. Item
        from .items import Item
        return Item

    def from_xml(self, elem, account):
        from .items import ITEM_CLASSES
        for item_cls in ITEM_CLASSES:
            item_elem = elem.find(item_cls.response_tag())
            if item_elem is not None:
                return item_cls.from_xml(elem=item_elem, account=account)

    def to_xml(self, value, version):
        # We don't want to wrap in an Item element
        return value.to_xml(version=version)
