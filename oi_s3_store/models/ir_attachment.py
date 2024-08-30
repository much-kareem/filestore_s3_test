'''
Created on Jan 25, 2017

@author: Zuhair Hammadi
'''
from odoo import models, fields, api, registry, tools, _, SUPERUSER_ID
from odoo.tools import config
from odoo.exceptions import AccessError, UserError
import boto3
import logging
import os
import time
import datetime
from collections import defaultdict
import shutil
from pytz import UTC as utc

_logger = logging.getLogger(__name__)

def sizeof_fmt(num, suffix='B'):
    for unit in ['','Ki','Mi','Gi','Ti','Pi','Ei','Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)

class IrAttachment(models.Model):
    _inherit = 'ir.attachment'
    
    s3_key = fields.Char(index = True)
    content_location = fields.Selection([('db','Database'), ('file', 'File System'), ('s3', 'S3 Object Storage')], compute = '_calc_content_location')

    @api.depends('s3_key', 'store_fname')
    def _calc_content_location(self):
        for record in self:
            if isinstance(record.id, models.NewId):
                record.content_location = self._storage()
            elif record.s3_key:
                record.content_location = 's3'
            elif record.store_fname:
                record.content_location = 'file'
            else:
                record.content_location = 'db'                                

    def _move_content(self, attachment_storage):
        self = self.with_context(attachment_storage = attachment_storage)
        if attachment_storage != 's3':
            self._unlink_s3()
        for attach in self:
            if attach.content_location == attachment_storage:
                continue
            attach.write({'raw': attach.raw, 'mimetype': attach.mimetype})    
    
    @api.model
    def force_storage(self, limit = 100):
        """Force all attachments to be stored in the currently configured storage"""
        if self._uid != SUPERUSER_ID:
            raise AccessError(_('Only administrators can execute this action.'))

        # domain to retrieve the attachments to migrate
        storage = self._storage()
        domain = {
            'db': ['|', ('store_fname', '!=', False), ('s3_key', '!=', False)],
            'file': ['|', ('db_datas', '!=', False), ('s3_key', '!=', False)],
            's3': ['|', ('db_datas', '!=', False), ('store_fname', '!=', False)],
        }[storage]
        
        domain = [('id','!=', 0)] + domain
        
        records = self.search(domain, limit = limit, order = 'id')
        
        _logger.info('Force Storage for %d records, id [%d, %d]' % (len(records), records[:1].id, records[-1:].id ))

        for attach in self.search(domain):
            attach.write({'raw': attach.raw, 'mimetype': attach.mimetype})
                                        
        remain = self.search(domain, count = True)
        
        if remain:
            _logger.info('Force Storage : remain %s records ' % remain)
        
        self._s3_cache_gc(hours = 1)
            
        return remain                   
    
    @api.model
    def get_s3_client(self):
        get_param = self.env['ir.config_parameter'].sudo().get_param
        
        region_name = get_param('ir_attachment.aws_region_name') or config.get('aws_region_name')
        api_version = get_param('ir_attachment.aws_api_version') or config.get('aws_api_version')
        use_ssl = bool(get_param('ir_attachment.aws_use_ssl') or config.get('aws_use_ssl'))
        verify = get_param('ir_attachment.aws_verify') or config.get('aws_verify')
        endpoint_url = get_param('ir_attachment.aws_endpoint_url') or config.get('aws_endpoint_url')
        aws_access_key_id = get_param('ir_attachment.aws_access_key_id') or config.get('aws_access_key_id')
        aws_secret_access_key = get_param('ir_attachment.aws_secret_access_key') or config.get('aws_secret_access_key')
        
        if verify == 'False':
            verify = False
        
        return boto3.client('s3', region_name = region_name, api_version = api_version, use_ssl = use_ssl, verify = verify, 
                            endpoint_url = endpoint_url, aws_access_key_id = aws_access_key_id, aws_secret_access_key = aws_secret_access_key)       
    
    @api.model
    def _s3_cache(self):
        return config.get("s3_cache_dir") or os.path.join(config['data_dir'], 's3_cache')     
    
    @api.model  
    def _s3_cache_file(self, s3_key):
        return os.path.join(self._s3_cache(), *s3_key.split('/'))
    
    @api.model  
    def _s3_file_write(self, filename, data):
        if not filename:
            return
        dirname = os.path.dirname(filename)
        if not os.path.isdir(dirname):
            with tools.ignore(OSError):
                os.makedirs(dirname)         
        with open(filename, 'wb') as f:
            f.write(data)               
    
    @api.model  
    def _s3_bucket(self):
        res= self.env['ir.config_parameter'].sudo().get_param("ir_attachment.s3_bucket") or config.get('s3_bucket')
        if not res:
            raise UserError ('Please set config s3_bucket')                
        return res
    
    @api.model    
    def _s3_read(self, key):
        s3_bucket = self._s3_bucket()
        s3_key = '%s/%s' % (self._cr.dbname, key )
        try:
            cache_file = self._s3_cache_file(s3_key)
            if os.path.isfile(cache_file):
                mtime = time.time()
                os.utime(cache_file, (mtime, mtime))
                value= open(cache_file,'rb').read()
            else:            
                value= self.get_s3_client().get_object(Bucket=s3_bucket, Key=s3_key)['Body'].read()
                if self.env['ir.config_parameter'].sudo().get_param('ir_attachment.s3_cache') == 'True':  
                    self._s3_file_write(cache_file, value) 
            return value
        except Exception as e:
            _logger.error('_s3_read error %s %s' % (s3_key, type(e)))
            _logger.exception(e)
            return b''        
    
    @api.model    
    def _s3_write(self, value, checksum):
        s3_bucket = self._s3_bucket()
        
        key = '%s/%s/%s' % (checksum[:2],checksum[2:4],checksum )
        s3_key = '%s/%s' % (self._cr.dbname, key )        
        
        self.get_s3_client().put_object(Bucket=s3_bucket, Key=s3_key, Body=value)
       
        if self.env['ir.config_parameter'].sudo().get_param('ir_attachment.s3_cache') == 'True':        
            cache_file = self._s3_cache_file(s3_key)
            self._s3_file_write(cache_file, value) 

        return key    
                      
    @api.depends('store_fname', 'db_datas', 's3_key')
    def _compute_raw(self):
        super(IrAttachment, self)._compute_raw()
        for attach in self:
            if attach.s3_key:
                attach.raw = self._s3_read(attach.s3_key)            
                                                                    
    def _recompute_mimetype(self):
        for record in self:
            mimetype = record.mimetype
            for fname in ['name', 'url', 'raw']:
                vals, = record.read([fname])
                mimetype = record._compute_mimetype(vals)
                if mimetype !='application/octet-stream':
                    break
            record.write({'mimetype' : mimetype})    
            
    def _get_datas_related_values(self, data, mimetype):
        values = {
            'file_size': len(data),
            'checksum': self._compute_checksum(data),
            'index_content': self._index(data, mimetype),
            'store_fname': False,
            's3_key' : False,
            'db_datas': False,
        }
        if data:
            location = self._context.get('attachment_storage') or self._storage()        
            if location == 'file':
                values['store_fname'] = self._file_write(data, values['checksum'])
            elif location == 's3':
                values['s3_key'] = self._s3_write(data, values['checksum'])
            else:
                values['db_datas'] = data         
        else:
            values['db_datas'] = data   
            
        return values
                                    
    @api.autovacuum
    def _s3_cache_gc(self, hours = -24):        
        s3_cache_folder = self._s3_cache()
        if not os.path.exists(s3_cache_folder):
            return
        from_timestamp = (datetime.datetime.now() + datetime.timedelta(hours = hours)).timetuple()
        from_timestamp = time.mktime(from_timestamp)        
        count = defaultdict(int)
                        
        for root, dirs, files in os.walk(s3_cache_folder):
            for fname in files:        
                full_path = os.path.join(root, fname)
                if os.path.getmtime(full_path) >= from_timestamp:
                    continue
                try:
                    os.unlink(full_path)       
                    count['file'] +=1         
                except (OSError, IOError):
                    _logger.info("_s3_cache_gc could not unlink %s", full_path, exc_info=True)
                
            for dname in dirs:
                full_path = os.path.join(root, dname)
                if not os.listdir(full_path):
                    try:
                        shutil.rmtree(full_path)
                        count['dir'] +=1  
                    except (OSError, IOError):
                        _logger.info("_s3_cache_gc could not unlink %s", full_path, exc_info=True)
                                                         
        _logger.info('S3 cache delete %(file)d files, %(dir)d directories' % count)                
    
    @api.ondelete(at_uninstall=False)
    def _unlink_s3(self):
        if self.env['ir.config_parameter'].sudo().get_param('ir_attachment.s3_delete') != 'True':
            return
        s3_keys = list(filter(None,self.mapped("s3_key")))        
        if not s3_keys:
            return
        dbname = self.env.cr.dbname            

        @self.env.cr.postcommit.add
        def delete_objects():
            db_registry = registry(dbname)
            with api.Environment.manage(), db_registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                cr.execute("select s3_key from ir_attachment where s3_key in %s", [tuple(s3_keys)])                    
                for s3_key, in cr.fetchall():
                    s3_keys.remove(s3_key)

                if not s3_keys:
                    return
                self = env['ir.attachment']
                client = self.get_s3_client()
                response = client.delete_objects(
                    Bucket= self._s3_bucket(),
                    Delete={
                        'Objects': list(map(lambda key: {"Key" : f"{dbname}/{key}"}, s3_keys)),
                        'Quiet': True
                    },
                )           
