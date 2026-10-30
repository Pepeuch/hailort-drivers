"""Additional installer checks. Root, DKMS and signing are fixture-only."""
import errno,hashlib,io,os,unittest
from contextlib import redirect_stdout,redirect_stderr
from unittest.mock import patch
import test_install as base
installer=base.installer
class ReviewTests(base.InstallTests):
    def signing_sentinels(self):
        data={}
        for name in ['etc/dkms/framework.conf','var/lib/dkms/mok.key','var/lib/dkms/mok.pub']:
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(('fixture only '+name).encode());p.chmod(0o600);data[p]=(p.read_bytes(),p.stat().st_mode)
        return data
    def assert_signing_untouched(self,data):
        for p,before in data.items():self.assertEqual(before,(p.read_bytes(),p.stat().st_mode))
    def test_review_signing_failure_stops_before_install_and_retains_recovery(self):
        data=self.signing_sentinels();original=installer.run_dkms
        def signing_failure(command,log):
            if command[1]=='build':log.write('fixture-only sign-file failure\n');log.flush()
            return original(command,log)
        with patch.dict(os.environ,INSTALL_FAIL='build'),patch.object(installer,'run_dkms',side_effect=signing_failure):code,output=self.invoke()
        self.assertEqual(17,code);self.assertEqual(2,len(self.calls()));self.assertNotIn('install -m', '\n'.join(self.calls()));self.assertIn('No rollback attempted',output)
        self.assertIn('fixture-only sign-file failure',(self.destination()/'.hailo-install.log').read_text());self.assert_signing_untouched(data)
    def test_review_success_preserves_signing_configuration_and_key_material(self):
        data=self.signing_sentinels();self.assertEqual(0,self.invoke()[0]);self.assert_signing_untouched(data)
        for call in self.calls():self.assertNotIn('--force',call);self.assertNotIn('--no-sign',call)
    def test_review_staged_modes_strip_special_and_writable_bits(self):
        p=self.root/'input/includes/mode-test.h';p.write_text('fixture header');p.chmod(0o6777)
        self.assertEqual(0,self.invoke()[0]);out=self.destination()/'common/include/mode-test.h'
        self.assertEqual(0o755,out.stat().st_mode & 0o7777);self.assertEqual(p.read_bytes(),out.read_bytes())
    def test_review_cleanup_error_after_install_keeps_completed_state(self):
        out=io.StringIO()
        with redirect_stdout(out),redirect_stderr(out),patch.object(installer.shutil,'rmtree',side_effect=OSError(errno.EIO,'fixture cleanup failed')):
            with self.assertRaises(OSError):installer.install(self.args)
        self.assertEqual(3,len(self.calls()));self.assertTrue((self.root/'dkms/hailo_pci/4.24.0/install.kernel-a').exists())
        self.assertIn('Installer stopped at: complete',(self.destination()/'.hailo-install.log').read_text())
        self.assertEqual(1,len(list((self.root/'sources').glob('*.stage-*'))))
        for p,data in self.before.items():self.assertEqual(data,p.read_bytes())
def load_tests(loader,tests,pattern):
    return unittest.TestSuite(ReviewTests(name) for name in sorted(dir(ReviewTests)) if name.startswith('test_review_'))
if __name__=='__main__':unittest.main()
