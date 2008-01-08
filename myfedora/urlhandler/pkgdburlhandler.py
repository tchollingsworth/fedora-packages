from urlhandler import URLHandler

class PkgDBURLHandler(URLHandler):
    def __init__(self):
        URLHandler.__init__(self)

        self.set_base_url('https://admin.fedoraproject.org/pkgdb/packages/name/')
