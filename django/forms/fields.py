<!-- ... existing code ... -->
import math
import operator
import os
import re
import uuid
from decimal import Decimal, DecimalException
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

from django.core import validators
from django.core.exceptions import ValidationError
from django.db.models.utils import get_blank_choice_label
<!-- ... existing code ... -->
    "TypedMultipleChoiceField",
    "UUIDField",
)


class Field:
    widget: Type[Any] = TextInput  # Default widget to use when rendering this type of Field.
    hidden_widget: Type[Any] = HiddenInput  # Default widget to use when rendering this as "hidden".
    default_validators: List[Callable] = []  # Default set of validators
    # Add an 'invalid' entry to default_error_message if you want a specific
    # field error message not raised by the field validators.
    default_error_messages: Dict[str, str] = {
        "required": _("This field is required."),
    }
    empty_values: List[Any] = list(validators.EMPTY_VALUES)
    bound_field_class: Optional[Type[BoundField]] = None

    def __init__(
        self,
        *,
        required: bool = True,
        widget: Optional[Union[Type[Any], Any]] = None,
        label: Optional[str] = None,
        initial: Any = None,
        help_text: str = "",
        error_messages: Optional[Dict[str, str]] = None,
        show_hidden_initial: bool = False,
        validators: Tuple[Callable, ...] = (),
        localize: bool = False,
        disabled: bool = False,
        label_suffix: Optional[str] = None,
        template_name: Optional[str] = None,
        bound_field_class: Optional[Type[BoundField]] = None,
    ) -> None:
        # required -- Boolean that specifies whether the field is required.
        #             True by default.
        # widget -- A Widget class, or instance of a Widget class, that should
        #           be used for this Field when displaying it. Each Field has a
<!-- ... existing code ... -->
        self.bound_field_class = bound_field_class or self.bound_field_class
        
        # Initialize widget
        actual_widget = widget or self.widget
        if isinstance(actual_widget, type):
            self.widget = actual_widget()
        else:
            self.widget = copy.deepcopy(actual_widget)

        # Trigger the localization machinery if needed.
        self.localize = localize
        if self.localize:
            self.widget.is_localized = True

        # Let the widget know whether it should display as required.
        self.widget.is_required = self.required

        # Hook into self.widget_attrs() for any Field-specific HTML attributes.
        extra_attrs = self.widget_attrs(self.widget)
        if extra_attrs:
            self.widget.attrs.update(extra_attrs)

        # Setup error messages
        messages = {}
        for c in reversed(self.__class__.__mro__):
            messages.update(getattr(c, "default_error_messages", {}))
        messages.update(error_messages or {})
        self.error_messages = messages

        self.validators = [*self.default_validators, *validators]
        self.template_name = template_name

    def prepare_value(self, value: Any) -> Any:
        return value

    def to_python(self, value: Any) -> Any:
        return value

    def validate(self, value: Any) -> None:
        if value in self.empty_values and self.required:
            raise ValidationError(self.error_messages["required"], code="required")

    def run_validators(self, value: Any) -> None:
        if value in self.empty_values:
            return
        errors = []
        for v in self.validators:
            try:
                v(value)
            except ValidationError as e:
                if hasattr(e, "code") and e.code in self.error_messages:
                    e.message = self.error_messages[e.code]
                errors.extend(e.error_list)
        if errors:
            raise ValidationError(errors)

    def clean(self, value: Any) -> Any:
        """
        Validate the given value and return its "cleaned" value as an
        appropriate Python object. Raise ValidationError for any errors.
        """
        value = self.to_python(value)
        self.validate(value)
        self.run_validators(value)
        return value

    def bound_data(self, data: Any, initial: Any) -> Any:
        """
        Return the value that should be shown for this field on render of a
        bound form, given the submitted POST data for the field and the initial
        data, if any.
<!-- ... existing code ... -->
        """
        if self.disabled:
            return initial
        return data

    def widget_attrs(self, widget: Any) -> Dict[str, Any]:
        """
        Given a Widget instance (*not* a Widget class), return a dictionary of
        any HTML attributes that should be added to the Widget, based on this
        Field.
        """
        return {}

    def has_changed(self, initial: Any, data: Any) -> bool:
        """Return True if data differs from initial."""
        # Always return False if the field is disabled since self.bound_data
        # always uses the initial value in this case.
        if self.disabled:
            return False
        try:
            data = self.to_python(data)
            if hasattr(self, "_coerce"):
                return self._coerce(data) != self._coerce(initial)
        except ValidationError:
            return True
            
        # For purposes of seeing whether something has changed, None is
        # the same as an empty string. Replace None with '' for comparison.
        initial_value = initial if initial is not None else ""
        data_value = data if data is not None else ""
        return initial_value != data_value

    def get_bound_field(self, form: Any, field_name: str) -> BoundField:
        """
        Return a BoundField instance that will be used when accessing the form
        field in a template.
        """
        bound_field_class = (
            self.bound_field_class or getattr(form, 'bound_field_class', BoundField)
        )
        return bound_field_class(form, self, field_name)

    def __deepcopy__(self, memo: Dict[int, Any]) -> "Field":
        result = copy.copy(self)
        memo[id(self)] = result
        result.widget = copy.deepcopy(self.widget, memo)
        result.error_messages = self.error_messages.copy()
        result.validators = self.validators[:]
        return result

    def _clean_bound_field(self, bf: BoundField) -> Any:
        value = bf.initial if self.disabled else bf.data
        return self.clean(value)


class CharField(Field):
    def __init__(
        self, 
        *, 
        max_length: Optional[int] = None, 
        min_length: Optional[int] = None, 
        strip: bool = True, 
        empty_value: str = "", 
        **kwargs: Any
    ) -> None:
        self.max_length = max_length
        self.min_length = min_length
        self.strip = strip
        self.empty_value = empty_value
        super().__init__(**kwargs)
        if min_length is not None:
            self.validators.append(validators.MinLengthValidator(int(min_length)))
        if max_length is not None:
            self.validators.append(validators.MaxLengthValidator(int(max_length)))
        self.validators.append(validators.ProhibitNullCharactersValidator())

    def to_python(self, value: Any) -> str:
        """Return a string."""
        if value not in self.empty_values:
            value = str(value)
            if self.strip:
                value = value.strip()
        
        if value in self.empty_values:
            return self.empty_value
        return value

    def widget_attrs(self, widget: Any) -> Dict[str, Any]:
        attrs = super().widget_attrs(widget)
        if self.max_length is not None and not widget.is_hidden:
            # The HTML attribute is maxlength, not max_length.
            attrs["maxlength"] = str(self.max_length)
        if self.min_length is not None and not widget.is_hidden:
            # The HTML attribute is minlength, not min_length.
            attrs["minlength"] = str(self.min_length)
        return attrs


class IntegerField(Field):
    widget = NumberInput
    default_error_messages = {
        "invalid": _("Enter a whole number."),
    }
    re_decimal = _lazy_re_compile(r"\.0*\s*$")

    def __init__(
        self, 
        *, 
        max_value: Optional[int] = None, 
        min_value: Optional[int] = None, 
        step_size: Optional[int] = None, 
        **kwargs: Any
    ) -> None:
        self.max_value, self.min_value, self.step_size = max_value, min_value, step_size
        if kwargs.get("localize") and kwargs.get("widget", self.widget) == NumberInput:
            # Localized number input is not well supported on most browsers
            kwargs.setdefault("widget", super().widget)
        super().__init__(**kwargs)

        if max_value is not None:
            self.validators.append(validators.MaxValueValidator(max_value))
        if min_value is not None:
            self.validators.append(validators.MinValueValidator(min_value))
        if step_size is not None:
            self.validators.append(
                validators.StepValueValidator(step_size, offset=min_value)
            )

    def to_python(self, value: Any) -> Optional[int]:
        """
        Validate that int() can be called on the input. Return the result
        of int() or None for empty values.
        """
        value = super().to_python(value)
        if value in self.empty_values:
            return None
        if self.localize:
            value = formats.sanitize_separators(value)
        # Strip trailing decimal and zeros.
        try:
            value = int(self.re_decimal.sub("", str(value)))
        except (ValueError, TypeError):
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        return value

    def widget_attrs(self, widget: Any) -> Dict[str, Any]:
        attrs = super().widget_attrs(widget)
        if isinstance(widget, NumberInput):
            if self.min_value is not None:
                attrs["min"] = str(self.min_value)
            if self.max_value is not None:
                attrs["max"] = str(self.max_value)
            if self.step_size is not None:
                attrs["step"] = str(self.step_size)
        return attrs


class FloatField(IntegerField):
    default_error_messages = {
        "invalid": _("Enter a number."),
    }

    def to_python(self, value: Any) -> Optional[float]:
        """
        Validate that float() can be called on the input. Return the result
        of float() or None for empty values.
        """
        value = super(IntegerField, self).to_python(value)
        if value in self.empty_values:
            return None
        if self.localize:
            value = formats.sanitize_separators(value)
        try:
            value = float(value)
        except (ValueError, TypeError):
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        return value

    def validate(self, value: Optional[float]) -> None:
        super().validate(value)
        if value in self.empty_values:
            return
        if value is not None and not math.isfinite(value):
            raise ValidationError(self.error_messages["invalid"], code="invalid")

    def widget_attrs(self, widget: Any) -> Dict[str, Any]:
        attrs = super().widget_attrs(widget)
        if isinstance(widget, NumberInput) and "step" not in widget.attrs:
            if self.step_size is not None:
                step = str(self.step_size)
            else:
                step = "any"
            attrs.setdefault("step", step)
        return attrs


class DecimalField(IntegerField):
    default_error_messages = {
        "invalid": _("Enter a number."),
    }

    def __init__(
        self,
        *,
        max_value: Optional[Decimal] = None,
        min_value: Optional[Decimal] = None,
        max_digits: Optional[int] = None,
        decimal_places: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        self.max_digits, self.decimal_places = max_digits, decimal_places
        super().__init__(max_value=max_value, min_value=min_value, **kwargs)
        self.validators.append(validators.DecimalValidator(max_digits, decimal_places))

    def to_python(self, value: Any) -> Optional[Decimal]:
        """
        Validate that the input is a decimal number. Return a Decimal
        instance or None for empty values. Ensure that there are no more
        than max_digits in the number and no more than decimal_places digits
        after the decimal point.
        """
        if value in self.empty_values:
            return None
        if self.localize:
            value = formats.sanitize_separators(value)
        try:
            value = Decimal(str(value))
        except DecimalException:
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        return value

    def validate(self, value: Optional[Decimal]) -> None:
        super().validate(value)
        if value in self.empty_values:
            return
        if value is not None and not value.is_finite():
            raise ValidationError(
                self.error_messages["invalid"],
                code="invalid",
                params={"value": value},
            )

    def widget_attrs(self, widget: Any) -> Dict[str, Any]:
        attrs = super().widget_attrs(widget)
        if isinstance(widget, NumberInput) and "step" not in widget.attrs:
            if self.decimal_places is not None:
                # Use exponential notation for small values since they might
                # be parsed as 0 otherwise. ref #20765
                step = str(Decimal(1).scaleb(-self.decimal_places)).lower()
            else:
                step = "any"
            attrs.setdefault("step", step)
        return attrs


class BaseTemporalField(Field):
    def __init__(self, *, input_formats: Optional[List[str]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if input_formats is not None:
            self.input_formats = input_formats

    def to_python(self, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
        # Try to strptime against each input format.
        for format in self.input_formats:
            try:
                return self.strptime(value, format)
            except (ValueError, TypeError):
                continue
        raise ValidationError(self.error_messages["invalid"], code="invalid")

    def strptime(self, value: str, format: str) -> Any:
        raise NotImplementedError("Subclasses must define this method.")
<!-- ... existing code ... -->
