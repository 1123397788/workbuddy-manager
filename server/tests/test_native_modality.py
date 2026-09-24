import unittest
from server.services import modelcatalog

class NativeModalityTests(unittest.TestCase):
 def test_platform_image_flag_does_not_override_native_type(self):
  for mid,want in [('glm-5.3','text'),('glm-5.3-flash','multimodal'),('deepseek-v4-pro','text'),('auto','router'),('new-model','unknown')]:
   with self.subTest(mid=mid):
    item=modelcatalog._decorate([{'id':mid,'supports_images':True}])[0]
    self.assertEqual(item.get('native_modality'),want)
 def test_source_and_date_are_exposed(self):
  item=modelcatalog._decorate([{'id':'glm-5.3'}])[0]
  self.assertTrue(item.get('native_modality_source','').startswith('https://docs.bigmodel.cn/'))
  self.assertEqual(item.get('native_modality_verified_at'),'2026-09-25')
 def test_unknown_alias_not_inferred(self):
  for mid in ['glm-5.3-unknown','hy4-preview-f','kimi-k2.8-preview']:
   self.assertEqual(modelcatalog._decorate([{'id':mid,'supports_images':True}])[0].get('native_modality'),'unknown')
