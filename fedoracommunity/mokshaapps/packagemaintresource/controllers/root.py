from moksha.lib.base import Controller
from moksha.lib.helpers import MokshaApp
from tg import expose, tmpl_context
from fedoracommunity.widgets import SubTabbedContainer

class TabbedNav(SubTabbedContainer):
    tabs= (MokshaApp('Builds', 'fedoracommunity.builds'),
           MokshaApp('Updates', 'fedoracommunity.packagemaint.updates'),
           MokshaApp('Packages', 'fedoracommunity.packagemaint.packages'),
           MokshaApp('Package Groups', 'fedoracommunity.packagemaint.packagegroups'),
          )

class RootController(Controller):
    def __init__(self):
        self.widget = TabbedNav('packagemaintnav')

    @expose('mako:fedoracommunity.mokshaapps.packagemaintresource.templates.index')
    def index(self):
        tmpl_context.widget = self.widget
        return {}
