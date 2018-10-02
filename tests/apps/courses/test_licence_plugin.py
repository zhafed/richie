"""
Licence plugin tests
"""
from django.db import transaction

from cms.api import add_plugin
from cms.models import Placeholder

from richie.apps.core.tests.utils import CMSPluginTestCase
from richie.apps.courses.cms_plugins import LicencePlugin
from richie.apps.courses.factories import LicenceFactory


# pylint: disable=too-many-ancestors
class LicencePluginTestCase(CMSPluginTestCase):
    """Licence plugin tests case"""

    @transaction.atomic
    def test_section_context_and_html(self):
        """
        Instanciating this plugin with an instance should populate the context
        and render in the template.
        """
        placeholder = Placeholder.objects.create(slot="test")

        # Create random values for parameters with a factory
        licence = LicenceFactory()

        model_instance = add_plugin(placeholder, LicencePlugin, "en", licence=licence)
        plugin_instance = model_instance.get_plugin_class_instance()
        plugin_context = plugin_instance.render({}, model_instance, None)

        # Check if "instance" is in plugin context
        self.assertIn("instance", plugin_context)

        # Check if parameters, generated by the factory, are correctly set in
        # "instance" of plugin context
        self.assertEqual(plugin_context["instance"].licence.name, licence.name)

        # Template context
        context = self.get_practical_plugin_context()

        # Get generated html for licence name
        html = context["cms_content_renderer"].render_plugin(model_instance, {})

        # Check rendered name
        self.assertIn(licence.name, html)

    @transaction.atomic
    def test_section_header_level(self):
        """
        Header level can be changed from context variable 'header_level'.
        """
        # We deliberately use level '10' since it can be substituted from any
        # reasonable default level.
        header_format = """<h10 class="licence-plugin__wrapper__title">{}</h10>"""

        # Dummy slot where to include plugin
        placeholder = Placeholder.objects.create(slot="test")

        # Create random values for parameters with a factory, empty url to
        # simplify render of tested HTML
        licence = LicenceFactory(url="")

        # Template context with additional variable to define a custom header
        # level for header markup
        context = self.get_practical_plugin_context({"header_level": 10})

        # Init base Section plugin with required title
        add_plugin(placeholder, LicencePlugin, "en", licence=licence)

        # Render placeholder so plugin is fully rendered in real situation
        html = context["cms_content_renderer"].render_placeholder(
            placeholder, context=context, language="en"
        )

        expected_header = header_format.format(licence.name)

        # Expected header markup should match given 'header_level' context
        # variable
        self.assertInHTML(expected_header, html)
