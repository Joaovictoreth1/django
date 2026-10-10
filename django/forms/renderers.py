import functools
from pathlib import Path
from typing import Any, Dict, Optional, Type

from django.conf import settings
# Renamed import to avoid shadowing the DjangoTemplates renderer class below
from django.template.backends.django import DjangoTemplates as DjangoTemplatesBackend
from django.template.loader import get_template
from django.utils.functional import cached_property
from django.utils.module_loading import import_string

@functools.lru_cache(maxsize=None)
def get_default_renderer() -> "BaseRenderer":
    """
    Return an instance of the default form renderer specified in settings.
    Caches the instantiated renderer to avoid repeated imports and instantiations.
    """
    renderer_class: Type["BaseRenderer"] = import_string(settings.FORM_RENDERER)
    return renderer_class()

class BaseRenderer:
    """
    Abstract base class for all form renderers.
    Defines the default template names and the rendering interface.
    """
    form_template_name: str = "django/forms/div.html"
    formset_template_name: str = "django/forms/formsets/div.html"
    field_template_name: str = "django/forms/field.html"

    bound_field_class: Optional[Type[Any]] = None

    def get_template(self, template_name: str) -> Any:
        """
        Load and return a template for the given name.
        """
        raise NotImplementedError("subclasses must implement get_template()")

    def render(
        self, template_name: str, context: Dict[str, Any], request: Optional[Any] = None
    ) -> str:
        """
        Render a template with a given context and return the rendered string.
        """
        template = self.get_template(template_name)
        return template.render(context, request=request).strip()

class EngineMixin:
    """
    Mixin that provides a configured template engine instance for rendering.
    """
    backend: Type[Any]  # Must be defined by subclasses

    def get_template(self, template_name: str) -> Any:
        """Fetch the template directly from the configured engine."""
        return self.engine.get_template(template_name)

    @cached_property
    def engine(self) -> Any:
        """
        Configure and return the template engine backend instance.
        """
        return self.backend(
            {
                "APP_DIRS": True,
                "DIRS": [Path(__file__).parent / self.backend.app_dirname],
                "NAME": "djangoforms",
                "OPTIONS": {},
            }
        )

class DjangoTemplates(EngineMixin, BaseRenderer):
    """
    Load Django templates from the built-in widget templates in
    django/forms/templates and from apps' 'templates' directory.
    """
    backend = DjangoTemplatesBackend


class Jinja2(EngineMixin, BaseRenderer):
    """
    Load Jinja2 templates from the built-in widget templates in
    django/forms/jinja2 and from apps' 'jinja2' directory.
    """
    
    @cached_property
    def backend(self) -> Type[Any]:
        # Local import to prevent hard dependency on Jinja2
        # Renamed locally to avoid shadowing the Jinja2 class
        from django.template.backends.jinja2 import Jinja2 as Jinja2Backend
        return Jinja2Backend


class TemplatesSetting(BaseRenderer):
    """
    Load templates using template.loader.get_template() which is configured
    based on settings.TEMPLATES.
    """

    def get_template(self, template_name: str) -> Any:
        return get_template(template_name)
