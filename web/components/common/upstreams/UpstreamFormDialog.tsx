'use client';

/**
 * 上游（账号池分组）的新增 / 编辑弹窗。
 *
 * 为什么抽成公共组件：这个表单有两个入口——「设置 → 上游」的管理面板，以及
 * 账号页的「添加分组」（分组的日常入口在账号页：加号、移号都在那儿发生）。
 * 两份拷贝会漂移，而这里每个字段都直接影响路由行为（地址错了请求打错地方、
 * 目录错了账号管到别的组去）。
 *
 * 编辑时**不回填明文 api_key**（接口只回脱敏值，见 routers/upstreams.py）：
 * 留空 = 不修改。
 */
import {useEffect, useState} from 'react';

import {errText, upstreamsApi} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {UpstreamEndpoint} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {Textarea} from '@/components/ui/textarea';

interface FormState {
  name: string;
  base_url: string;
  api_key: string;
  note: string;
  enabled: boolean;
  auth_dir: string;
  container: string;
}

const emptyForm: FormState = {
  name: '',
  base_url: '',
  api_key: '',
  note: '',
  enabled: true,
  auth_dir: '',
  container: '',
};

export function UpstreamFormDialog({
  open,
  onOpenChange,
  editing,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** null = 新建；非空 = 编辑该上游 */
  editing: UpstreamEndpoint | null;
  /** 保存成功后的回调（新建时拿到新行——账号页用它直接切到新分组） */
  onSaved?: (item: UpstreamEndpoint) => void;
}) {
  const t = useT();
  const [form, setForm] = useState<FormState>(emptyForm);
  const [busy, setBusy] = useState(false);

  // 打开时按 editing 重置：弹窗可能是「新建 → 取消 → 编辑」，不能残留上一次的内容
  useEffect(() => {
    if (!open) return;
    setForm(
      editing
        ? {
            name: editing.name,
            base_url: editing.base_url,
            api_key: '', // 不回填明文；留空 = 不修改
            note: editing.note || '',
            enabled: editing.enabled,
            auth_dir: editing.auth_dir || '',
            container: editing.container || '',
          }
        : emptyForm,
    );
  }, [open, editing]);

  async function submit() {
    if (busy) return;
    if (!form.name.trim() || !form.base_url.trim()) {
      notify.err(t('upstreams.nameUrlRequired'));
      return;
    }
    setBusy(true);
    try {
      const saved = editing
        ? await upstreamsApi.update(editing.id as number, form)
        : await upstreamsApi.create(form);
      notify.ok(editing ? t('upstreams.updated') : t('upstreams.created'));
      onOpenChange(false);
      onSaved?.(saved);
    } catch (e) {
      // 校验失败（目录不是绝对路径、容器名非法等）后端回 400 且给出原因——原样弹
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[440px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{editing ? t('upstreams.editTitle') : t('upstreams.addTitle')}</DialogTitle>
          <DialogDescription>{t('upstreams.formGroupHint')}</DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-3">
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('upstreams.fieldName')}</Label>
            <Input
              value={form.name}
              onChange={(e) => setForm({...form, name: e.target.value})}
              placeholder={t('upstreams.fieldNamePlaceholder')}
              maxLength={64}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('upstreams.fieldUrl')}</Label>
            <Input
              value={form.base_url}
              onChange={(e) => setForm({...form, base_url: e.target.value})}
              placeholder="http://127.0.0.1:7863"
              maxLength={500}
            />
            <p className="text-[10px] leading-4 text-muted-foreground">
              {t('upstreams.fieldUrlHint')}
            </p>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('upstreams.fieldKey')}</Label>
            <Input
              value={form.api_key}
              onChange={(e) => setForm({...form, api_key: e.target.value})}
              // 编辑时不回填明文（接口只回脱敏值），当前值显示在占位里，「留空 = 不修改」
              placeholder={editing && editing.has_key
                ? t('upstreams.fieldKeyKeep', {masked: editing.api_key_masked})
                : t('upstreams.fieldKeyPlaceholder')}
              maxLength={500}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('upstreams.fieldDir')}</Label>
            <Input
              value={form.auth_dir}
              onChange={(e) => setForm({...form, auth_dir: e.target.value})}
              placeholder={t('upstreams.fieldDirPlaceholder')}
              maxLength={500}
            />
            <p className="text-[10px] leading-4 text-muted-foreground">
              {t('upstreams.fieldDirHint')}
            </p>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('upstreams.fieldContainer')}</Label>
            <Input
              value={form.container}
              onChange={(e) => setForm({...form, container: e.target.value})}
              placeholder={t('upstreams.fieldContainerPlaceholder')}
              maxLength={64}
            />
            <p className="text-[10px] leading-4 text-muted-foreground">
              {t('upstreams.fieldContainerHint')}
            </p>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('upstreams.fieldNote')}</Label>
            <Textarea
              value={form.note}
              onChange={(e) => setForm({...form, note: e.target.value})}
              maxLength={200}
              rows={2}
            />
          </div>
          <div className="flex items-center justify-between">
            <Label className="text-[11px] text-muted-foreground">{t('upstreams.fieldEnabled')}</Label>
            <Switch checked={form.enabled} onCheckedChange={(v) => setForm({...form, enabled: v})} />
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" className="rounded-full" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button className="rounded-full" disabled={busy} onClick={submit}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
