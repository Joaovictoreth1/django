from typing import Any, Callable, Dict, Iterable, List, Optional, Type, Union

from django.core.exceptions import ValidationError
from django.forms.fields import BooleanField, IntegerField
from django.forms.forms import Form
from django.forms.renderers import get_default_renderer
from django.forms.utils import ErrorList, RenderableFormMixin
from django.forms.widgets import CheckboxInput, HiddenInput, NumberInput
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext_lazy

__all__ = ("BaseFormSet", "formset_factory", "all_valid")

# special field names
TOTAL_FORM_COUNT: str = "TOTAL_FORMS"
INITIAL_FORM_COUNT: str = "INITIAL_FORMS"
MIN_NUM_FORM_COUNT: str = "MIN_NUM_FORMS"
MAX_NUM_FORM_COUNT: str = "MAX_NUM_FORMS"
ORDERING_FIELD_NAME: str = "ORDER"
DELETION_FIELD_NAME: str = "DELETE"

# default minimum number of forms in a formset
DEFAULT_MIN_NUM: int = 0

# default maximum number of forms in a formset, to prevent memory exhaustion
DEFAULT_MAX_NUM: int = 1000


class ManagementForm(Form):
    """
    Keep track of how many form instances are displayed on the page. If adding
    new forms via JavaScript, you should increment the count field of this form
    as well.
    """

    TOTAL_FORMS = IntegerField(widget=HiddenInput)
    INITIAL_FORMS = IntegerField(widget=HiddenInput)
    # MIN_NUM_FORM_COUNT and MAX_NUM_FORM_COUNT are output with the rest of the
    # management form, but only for the convenience of client-side code. The
    # POST value of them returned from the client is not checked.
    MIN_NUM_FORMS = IntegerField(required=False, widget=HiddenInput)
    MAX_NUM_FORMS = IntegerField(required=False, widget=HiddenInput)

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        # When the management form is invalid, we don't know how many forms
        # were submitted.
        cleaned_data.setdefault(TOTAL_FORM_COUNT, 0)
        cleaned_data.setdefault(INITIAL_FORM_COUNT, 0)
        return cleaned_data


class BaseFormSet(RenderableFormMixin):
    """
    A collection of instances of the same Form class.
    """

    deletion_widget: Type[Any] = CheckboxInput
    ordering_widget: Type[Any] = NumberInput
    default_error_messages: Dict[str, Any] = {
        "missing_management_form": _(
            "ManagementForm data is missing or has been tampered with. Missing fields: "
            "%(field_names)s. You may need to file a bug report if the issue persists."
        ),
        "too_many_forms": ngettext_lazy(
            "Please submit at most %(num)d form.",
            "Please submit at most %(num)d forms.",
            "num",
        ),
        "too_few_forms": ngettext_lazy(
            "Please submit at least %(num)d form.",
            "Please submit at least %(num)d forms.",
            "num",
        ),
    }

    template_name_div: str = "django/forms/formsets/div.html"
    template_name_p: str = "django/forms/formsets/p.html"
    template_name_table: str = "django/forms/formsets/table.html"
    template_name_ul: str = "django/forms/formsets/ul.html"
    
    # These will be set by formset_factory
    form: Type[Form]
    extra: int
    can_order: bool
    can_delete: bool
    can_delete_extra: bool
    min_num: int
    max_num: int
    absolute_max: int
    validate_min: bool
    validate_max: bool

    def __init__(
        self,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        auto_id: str = "id_%s",
        prefix: Optional[str] = None,
        initial: Optional[List[Dict[str, Any]]] = None,
        error_class: Type[ErrorList] = ErrorList,
        form_kwargs: Optional[Dict[str, Any]] = None,
        error_messages: Optional[Dict[str, str]] = None,
    ) -> None:
        self.is_bound = data is not None or files is not None
        self.prefix = prefix or self.get_default_prefix()
        self.auto_id = auto_id
        self.data = data or {}
        self.files = files or {}
        self.initial = initial
        self.form_kwargs = form_kwargs or {}
        self.error_class = error_class
        self._errors: Optional[List[ErrorList]] = None
        self._non_form_errors: Optional[ErrorList] = None
        
        # Renderer configuration
        self.form_renderer = getattr(self, 'renderer', None)
        self.renderer = self.form_renderer or get_default_renderer()

        messages = {}
        for cls in reversed(type(self).__mro__):
            messages.update(getattr(cls, "default_error_messages", {}))
        if error_messages is not None:
            messages.update(error_messages)
        self.error_messages = messages

    def __iter__(self) -> Iterable[Form]:
        """Yield the forms in the order they should be rendered."""
        return iter(self.forms)

    def __getitem__(self, index: int) -> Form:
        """Return the form at the given index, based on the rendering order."""
        return self.forms[index]

    def __len__(self) -> int:
        return len(self.forms)

    def __bool__(self) -> bool:
        """
        Return True since all formsets have a management form which is not
        included in the length.
        """
        return True

    def __repr__(self) -> str:
        if self._errors is None:
            is_valid = "Unknown"
        else:
            is_valid = (
                self.is_bound
                and not self._non_form_errors
                and not any(form_errors for form_errors in self._errors)
            )
        return "<%s: bound=%s valid=%s total_forms=%s>" % (
            self.__class__.__qualname__,
            self.is_bound,
            is_valid,
            self.total_form_count(),
        )

    @cached_property
    def management_form(self) -> ManagementForm:
        """Return the ManagementForm instance for this FormSet."""
        if self.is_bound:
            form = ManagementForm(
                self.data,
                auto_id=self.auto_id,
                prefix=self.prefix,
                renderer=self.renderer,
            )
            form.full_clean()
        else:
            form = ManagementForm(
                auto_id=self.auto_id,
                prefix=self.prefix,
                initial={
                    TOTAL_FORM_COUNT: self.total_form_count(),
                    INITIAL_FORM_COUNT: self.initial_form_count(),
                    MIN_NUM_FORM_COUNT: getattr(self, 'min_num', 0),
                    MAX_NUM_FORM_COUNT: getattr(self, 'max_num', 1000),
                },
                renderer=self.renderer,
            )
        return form

    def total_form_count(self) -> int:
        """Return the total number of forms in this FormSet."""
        if self.is_bound:
            # return absolute_max if it is lower than the actual total form
            # count in the data; this is DoS protection to prevent clients
            # from forcing the server to instantiate arbitrary numbers of
            # forms
            absolute_max = getattr(self, 'absolute_max', 2000)
            return min(
                self.management_form.cleaned_data[TOTAL_FORM_COUNT], absolute_max
            )
        else:
            initial_forms = self.initial_form_count()
            min_num = getattr(self, 'min_num', 0)
            max_num = getattr(self, 'max_num', 1000)
            extra = getattr(self, 'extra', 1)
            
            total_forms = max(initial_forms, min_num) + extra
            # Allow all existing related objects/inlines to be displayed,
            # but don't allow extra beyond max_num.
            if initial_forms > max_num >= 0:
                total_forms = initial_forms
            elif total_forms > max_num >= 0:
                total_forms = max_num
        return total_forms

    def initial_form_count(self) -> int:
        """Return the number of forms that are required in this FormSet."""
        if self.is_bound:
            return self.management_form.cleaned_data[INITIAL_FORM_COUNT]
        else:
            # Use the length of the initial data if it's there, 0 otherwise.
            initial_forms = len(self.initial) if self.initial else 0
        return initial_forms

    @cached_property
    def forms(self) -> List[Form]:
        """Instantiate forms at first property access."""
        # DoS protection is included in total_form_count()
        return [
            self._construct_form(i, **self.get_form_kwargs(i))
            for i in range(self.total_form_count())
        ]

    def get_form_kwargs(self, index: Optional[int]) -> Dict[str, Any]:
        """
        Return additional keyword arguments for each individual formset form.

        index will be None if the form being constructed is a new empty
        form.
        """
        return self.form_kwargs.copy()

    def _construct_form(self, i: int, **kwargs: Any) -> Form:
        """Instantiate and return the i-th form instance in a formset."""
        defaults = {
            "auto_id": self.auto_id,
            "prefix": self.add_prefix(i),
            "error_class": self.error_class,
            # Don't render the HTML 'required' attribute as it may cause
            # incorrect validation for extra, optional, and deleted
            # forms in the formset.
            "use_required_attribute": False,
            "renderer": self.form_renderer,
        }
        if self.is_bound:
            defaults["data"] = self.data
            defaults["files"] = self.files
        if self.initial and "initial" not in kwargs:
            try:
                defaults["initial"] = self.initial[i]
            except IndexError:
                pass
        # Allow extra forms to be empty, unless they're part of
        # the minimum forms.
        if i >= self.initial_form_count() and i >= getattr(self, 'min_num', 0):
            defaults["empty_permitted"] = True
        defaults.update(kwargs)
        form = self.form(**defaults)
        self.add_fields(form, i)
        return form

    @property
    def initial_forms(self) -> List[Form]:
        """Return a list of all the initial forms in this formset."""
        return self.forms[: self.initial_form_count()]

    @property
    def extra_forms(self) -> List[Form]:
        """Return a list of all the extra forms in this formset."""
        return self.forms[self.initial_form_count() :]

    @property
    def empty_form(self) -> Form:
        form_kwargs = {
            **self.get_form_kwargs(None),
            "auto_id": self.auto_id,
            "prefix": self.add_prefix("__prefix__"),
            "empty_permitted": True,
            "use_required_attribute": False,
            "renderer": self.form_renderer,
        }
        form = self.form(**form_kwargs)
        self.add_fields(form, None)
        return form

    @property
    def cleaned_data(self) -> List[Dict[str, Any]]:
        """
        Return a list of form.cleaned_data dicts for every form in self.forms.
        """
        if not self.is_valid():
            raise AttributeError(
                "'%s' object has no attribute 'cleaned_data'" % self.__class__.__name__
            )
        return [form.cleaned_data for form in self.forms]

    @property
    def deleted_forms(self) -> List[Form]:
        """Return a list of forms that have been marked for deletion."""
        if not self.is_valid() or not getattr(self, 'can_delete', False):
            return []
        # construct _deleted_form_indexes which is just a list of form indexes
        # that have had their deletion widget set to True
        if not hasattr(self, "_deleted_form_indexes"):
            self._deleted_form_indexes = []
            for i, form in enumerate(self.forms):
                # If this is an extra form and hasn't changed, ignore it.
                if i >= self.initial_form_count() and not form.has_changed():
                    continue
                if self._should_delete_form(form):
                    self._deleted_form_indexes.append(i)
        return [self.forms[i] for i in self._deleted_form_indexes]

    @property
    def ordered_forms(self) -> List[Form]:
        """
        Return a list of form in the order specified by the incoming data.
        Raise an AttributeError if ordering is not allowed.
        """
        if not self.is_valid() or not getattr(self, 'can_order', False):
            raise AttributeError(
                "'%s' object has no attribute 'ordered_forms'" % self.__class__.__name__
            )
        # Construct _ordering, which is a list of (form_index,
        # order_field_value) tuples. After constructing this list, we'll sort
        # it by order_field_value so we have a way to get to the form indexes
        # in the order specified by the form data.
        if not hasattr(self, "_ordering"):
            self._ordering = []
            for i, form in enumerate(self.forms):
                # If this is an extra form and hasn't changed, ignore it.
                if i >= self.initial_form_count() and not form.has_changed():
                    continue
                # don't add data marked for deletion to self.ordered_data
                if getattr(self, 'can_delete', False) and self._should_delete_form(form):
                    continue
                self._ordering.append((i, form.cleaned_data[ORDERING_FIELD_NAME]))
            # After we're done populating self._ordering, sort it.
            # A sort function to order things numerically ascending, but
            # None should be sorted below anything else. Allowing None as
            # a comparison value makes it so we can leave ordering fields
            # blank.

            def compare_ordering_key(k: tuple) -> tuple:
                if k[1] is None:
                    return (1, 0)  # +infinity, larger than any number
                return (0, k[1])

            self._ordering.sort(key=compare_ordering_key)
        # Return a list of form.cleaned_data dicts in the order specified by
        # the form data.
        return [self.forms[i[0]] for i in self._ordering]

    @classmethod
    def get_default_prefix(cls) -> str:
        return "form"

    @classmethod
    def get_deletion_widget(cls) -> Type[Any]:
        return cls.deletion_widget

    @classmethod
    def get_ordering_widget(cls) -> Type[Any]:
        return cls.ordering_widget

    def non_form_errors(self) -> ErrorList:
        """
        Return an ErrorList of errors that aren't associated with a particular
        form -- i.e., from formset.clean(). Return an empty ErrorList if there
        are none.
        """
        if self._non_form_errors is None:
            self.full_clean()
        return self._non_form_errors

    @property
    def errors(self) -> List[ErrorList]:
        """Return a list of form.errors for every form in self.forms."""
        if self._errors is None:
            self.full_clean()
        return self._errors

    def total_error_count(self) -> int:
        """Return the number of errors across all forms in the formset."""
        return len(self.non_form_errors()) + sum(
            len(form_errors) for form_errors in self.errors
        )

    def _should_delete_form(self, form: Form) -> bool:
        """Return whether or not the form was marked for deletion."""
        return form.cleaned_data.get(DELETION_FIELD_NAME, False)

    def is_valid(self) -> bool:
        """Return True if every form in self.forms is valid."""
        if not self.is_bound:
            return False
        # Accessing errors triggers a full clean the first time only.
        self.errors
        # List comprehension ensures is_valid() is called for all forms, 
        # avoiding generator short-circuiting so that all errors are populated.
        forms_valid = all(
            [
                form.is_valid()
                for form in self.forms
                if not (getattr(self, 'can_delete', False) and self._should_delete_form(form))
            ]
        )
        return forms_valid and not self.non_form_errors()

    def full_clean(self) -> None:
        """
        Clean all of self.data and populate self._errors and
        self._non_form_errors.
        """
        self._errors = []
        self._non_form_errors = self.error_class(
            error_class="nonform", renderer=self.renderer
        )
        empty_forms_count = 0

        if not self.is_bound:  # Stop further processing.
            return

        if not self.management_form.is_valid():
            error = ValidationError(
                self.error_messages["missing_management_form"],
                params={
                    "field_names": ", ".join(
                        self.management_form.add_prefix(field_name)
                        for field_name in self.management_form.errors
                    ),
                },
                code="missing_management_form",
            )
            self._non_form_errors.append(error)

        for i, form in enumerate(self.forms):
            # Empty forms are unchanged forms beyond those with initial data.
            if not form.has_changed() and i >= self.initial_form_count():
                empty_forms_count += 1
            # Accessing errors calls full_clean() if necessary.
            # _should_delete_form() requires cleaned_data.
            form_errors = form.errors
            if getattr(self, 'can_delete', False) and self._should_delete_form(form):
                continue
            self._errors.append(form_errors)
        try:
            if (
                getattr(self, 'validate_max', False)
                and self.total_form_count() - len(self.deleted_forms) > getattr(self, 'max_num', 1000)
            ) or self.management_form.cleaned_data[
                TOTAL_FORM_COUNT
            ] > getattr(self, 'absolute_max', 2000):
                raise ValidationError(
                    self.error_messages["too_many_forms"] % {"num": getattr(self, 'max_num', 1000)},
                    code="too_many_forms",
                )
            if (
                getattr(self, 'validate_min', False)
                and self.total_form_count()
                - len(self.deleted_forms)
                - empty_forms_count
                < getattr(self, 'min_num', 0)
            ):
                raise ValidationError(
                    self.error_messages["too_few_forms"] % {"num": getattr(self, 'min_num', 0)},
                    code="too_few_forms",
                )
            # Give self.clean() a chance to do cross-form validation.
            self.clean()
        except ValidationError as e:
            self._non_form_errors = self.error_class(
                e.error_list,
                error_class="nonform",
                renderer=self.renderer,
            )

    def clean(self) -> None:
        """
        Hook for doing any extra formset-wide cleaning after Form.clean() has
        been called on every form. Any ValidationError raised by this method
        will not be associated with a particular form; it will be accessible
        via formset.non_form_errors()
        """
        pass

    def has_changed(self) -> bool:
        """Return True if data in any form differs from initial."""
        return any(form.has_changed() for form in self)

    def add_fields(self, form: Form, index: Optional[int]) -> None:
        """A hook for adding extra fields on to each form instance."""
        initial_form_count = self.initial_form_count()
        if getattr(self, 'can_order', False):
            # Only pre-fill the ordering field for initial forms.
            if index is not None and index < initial_form_count:
                form.fields[ORDERING_FIELD_NAME] = IntegerField(
                    label=_("Order"),
                    initial=index + 1,
                    required=False,
                    widget=self.get_ordering_widget(),
                )
            else:
                form.fields[ORDERING_FIELD_NAME] = IntegerField(
                    label=_("Order"),
                    required=False,
                    widget=self.get_ordering_widget(),
                )
        if getattr(self, 'can_delete', False) and (
            getattr(self, 'can_delete_extra', False) or (index is not None and index < initial_form_count)
        ):
            form.fields[DELETION_FIELD_NAME] = BooleanField(
                label=_("Delete"),
                required=False,
                widget=self.get_deletion_widget(),
            )

    def add_prefix(self, index: Union[int, str]) -> str:
        return "%s-%s" % (self.prefix, index)

    def is_multipart(self) -> bool:
        """
        Return True if the formset needs to be multipart, i.e. it
        has FileInput, or False otherwise.
        """
        if self.forms:
            return self.forms[0].is_multipart()
        else:
            return self.empty_form.is_multipart()

    @property
    def media(self) -> Any:
        # All the forms on a FormSet are the same, so you only need to
        # interrogate the first form for media.
        if self.forms:
            return self.forms[0].media
        else:
            return self.empty_form.media

    @property
    def template_name(self) -> str:
        return self.renderer.formset_template_name

    def get_context(self) -> Dict[str, Any]:
        return {"formset": self}


def formset_factory(
    form: Type[Form],
    formset: Type[BaseFormSet] = BaseFormSet,
    extra: int = 1,
    can_order: bool = False,
    can_delete: bool = False,
    max_num: Optional[int] = None,
    validate_max: bool = False,
    min_num: Optional[int] = None,
    validate_min: bool = False,
    absolute_max: Optional[int] = None,
    can_delete_extra: bool = True,
    renderer: Optional[Any] = None,
) -> Type[BaseFormSet]:
    """Return a FormSet for the given form class."""
    if min_num is None:
        min_num = DEFAULT_MIN_NUM
    if max_num is None:
        max_num = DEFAULT_MAX_NUM
    # absolute_max is a hard limit on forms instantiated, to prevent
    # memory-exhaustion attacks. Default to max_num + DEFAULT_MAX_NUM
    # (which is 2 * DEFAULT_MAX_NUM if max_num is None in the first place).
    if absolute_max is None:
        absolute_max = max_num + DEFAULT_MAX_NUM
    if max_num > absolute_max:
        raise ValueError("'absolute_max' must be greater or equal to 'max_num'.")
    attrs = {
        "form": form,
        "extra": extra,
        "can_order": can_order,
        "can_delete": can_delete,
        "can_delete_extra": can_delete_extra,
        "min_num": min_num,
        "max_num": max_num,
        "absolute_max": absolute_max,
        "validate_min": validate_min,
        "validate_max": validate_max,
        "renderer": renderer,
    }
    form_name = form.__name__
    if form_name.endswith("Form"):
        formset_name = form_name + "Set"
    else:
        formset_name = form_name + "FormSet"
    return type(formset_name, (formset,), attrs)


def all_valid(formsets: Iterable[BaseFormSet]) -> bool:
    """Validate every formset and return True if all are valid."""
    # List comprehension ensures is_valid() is called for all formsets,
    # preventing short-circuiting so errors are gathered for each set.
    return all([formset.is_valid() for formset in formsets])
