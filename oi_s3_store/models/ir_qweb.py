'''
Created on Jun 9, 2021

@author: Zuhair Hammadi
'''
from odoo import models

class IrQWeb(models.AbstractModel):
    _inherit = 'ir.qweb'
    
    def get_asset_bundle(self, bundle_name, files, env=None, css=True, js=True):
        env = env or getattr(self, 'env', None)
        if env and not env.context.get("asset_bundle"):
            env = env[self._name].with_context(asset_bundle = True, attachment_storage = 'db').env
        
        return super(IrQWeb, self).get_asset_bundle(bundle_name, files, env=env, css=css, js=js)
