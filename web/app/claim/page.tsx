'use client';

/**
 * 红包抽奖页（公开）。
 *
 * 为什么不放在 `(main)/` 下：那一组的 layout 会做两件事——检查登录态、
 * 渲染管理端导航栏。而这里是**给收到链接的人看的**（同事、朋友，多半没有
 * 账号），既不该被踢去登录页，也不该看到管理入口。
 *
 * 抽奖码从 URL 的 `?code=` 取。用 `window.location` 而不是 `useSearchParams`：
 * 静态导出下后者要求外面包一层 Suspense，而这个页面没有任何服务端内容，
 * 直接读 location 更省事（也避免多一个 loading 态）。
 */
import {useCallback, useEffect, useState} from 'react';
import {Gift, Copy, Check, Loader2} from 'lucide-react';
import {notify} from '@/lib/toast';
import {claimApi, errText} from '@/lib/api';
import type {ClaimInfo, DrawResult} from '@/lib/types';
import {fmtDateTime, fmtNumber} from '@/lib/format';
import {Button} from '@/components/ui/button';
import {useT} from '@/lib/i18n/provider';
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/animate-ui/radix/dialog';

export default function ClaimPage() {
  const t = useT();
  const [code, setCode] = useState('');
  const [info, setInfo] = useState<ClaimInfo | null>(null);
  const [got, setGot] = useState<DrawResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    const c = new URLSearchParams(window.location.search).get('code') || '';
    setCode(c);
    if (!c) {
      setErr(t('claim.noCode'));
      setLoading(false);
      return;
    }
    claimApi.info(c)
      .then(setInfo)
      .catch((e) => setErr(errText(e)))
      .finally(() => setLoading(false));
  }, [t]);

  const draw = useCallback(async () => {
    setBusy(true);
    setErr('');
    try {
      setGot(await claimApi.draw(code));
      // 抽完刷新一下元信息：剩余份数变了（也让「本机已领过」显示出来）
      claimApi.info(code).then(setInfo).catch(() => undefined);
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  }, [code]);

  /**
   * 关闭弹窗时**自动复制密钥**。
   *
   * 为什么这么做：明文密钥只显示这一次，而人看到中奖提示后很容易直接点关闭，
   * 回过头才发现没复制 —— 那时已经拿不回来了。自动复制是最后一道保险。
   * 复制失败（浏览器拒绝/非 HTTPS）要**如实提示**，不能假装成功：谎报成功
   * 会让用户以为剪贴板里有，实际粘出来是空的。
   */
  async function closeAndCopy() {
    const k = got?.key;
    setGot(null);
    if (!k) return;
    try {
      await navigator.clipboard.writeText(k);
      notify.ok(t('claim.autoCopied'), t('claim.autoCopiedDetail'));
    } catch {
      notify.warn(t('claim.autoCopyFailed'), t('claim.autoCopyFailedDetail'));
    }
  }

  /** 复制「上次领到的那份」（第二次打开时用）。 */
  async function copyMine() {
    const k = info?.my_key;
    if (!k) return;
    try {
      await navigator.clipboard.writeText(k);
      notify.ok(t('claim.copied'));
    } catch {
      notify.err(t('claim.copyFailed'));
    }
  }

  async function copyKey() {
    if (!got) return;
    try {
      await navigator.clipboard.writeText(got.key);
      notify.ok(t('claim.copied'));
    } catch {
      notify.err(t('claim.copyFailed'));
    }
  }

  /** 额度文案：积分显示两位小数、token 显示整数（与后端 fmtAmount 同口径）。 */
  function amountText(v: number, kind: string): string {
    return kind === 'token' ? fmtNumber(v) : v.toFixed(2);
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm rounded-[24px] bg-muted px-6 py-8 text-center">
        <Gift className="mx-auto mb-4 h-12 w-12 text-amber-500" />

        {loading ? (
          <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />{t('common.loading')}
          </div>
        ) : err ? (
          <>
            <div className="text-sm font-medium">{t('claim.failed')}</div>
            <p className="mt-2 text-xs leading-5 text-muted-foreground">{err}</p>
          </>
        ) : info ? (
          <>
            <div className="text-base font-medium">
              {info.title || t('claim.untitled')}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {t('claim.subtitle', {left: info.left, total: info.shares})}
            </p>
            {info.models.length > 0 && (
              <p className="mt-1 text-[11px] text-muted-foreground">
                {t('redPacket.modelsScoped', {list: info.models.join(', ')})}
              </p>
            )}
            <p className="mt-1 text-[11px] text-muted-foreground/70">
              {t('claim.expiresAt', {at: fmtDateTime(info.expires_at)})}
            </p>

            <div className="mt-6">
              {info.expired ? (
                <div className="text-xs text-amber-600 dark:text-amber-400">
                  {t('claim.expired')}
                </div>
              ) : info.left <= 0 ? (
                <div className="text-xs text-amber-600 dark:text-amber-400">
                  {t('claim.empty')}
                </div>
              ) : info.claimed && info.my_key ? (
                /* 已经领过：把上次那份**直接显示出来**。
                   关掉弹窗才想起没存是很常见的，而明文只显示那一次 ——
                   刷新就能找回来，比「请联系发红包的人」有用得多。
                   （后端只回「这个 IP 自己领的那一份」，不是别人的。） */
                <div className="text-left">
                  <div className="mb-2 rounded-full bg-amber-500/15 px-3 py-1 text-center text-xs text-amber-600 dark:text-amber-400">
                    {t('claim.alreadyWithKey')}
                  </div>
                  <div className="rounded-2xl bg-background px-4 py-4 text-center">
                    <div className="text-2xl font-semibold tabular-nums text-amber-600 dark:text-amber-400">
                      {amountText(info.my_amount ?? 0, info.quota_kind)}
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {info.quota_kind === 'token' ? t('claim.unitTokens') : t('claim.unitCredits')}
                    </div>
                  </div>
                  <div className="mt-2 break-all rounded-xl bg-background px-3 py-2 font-mono text-[11px] leading-5">
                    {info.my_key}
                  </div>
                  <Button
                    variant="outline"
                    className="mt-2 w-full rounded-full"
                    onClick={copyMine}
                  >
                    <Copy className="mr-1.5 h-4 w-4" />{t('claim.copy')}
                  </Button>
                </div>
              ) : info.claimed ? (
                <div className="text-xs text-muted-foreground">{t('claim.already')}</div>
              ) : (
                <Button
                  size="lg"
                  className="w-full rounded-full bg-amber-500 text-white hover:bg-amber-600"
                  disabled={busy}
                  onClick={draw}
                >
                  {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                  {t('claim.open')}
                </Button>
              )}
            </div>
          </>
        ) : null}
      </div>

      {/* 中奖弹窗：额度在上、密钥在下，关闭时自动复制 */}
      <Dialog open={!!got} onOpenChange={(v) => { if (!v) void closeAndCopy(); }}>
        <DialogContent className="max-w-sm rounded-[24px]">
          <DialogHeader>
            <DialogTitle className="text-center">{t('claim.won')}</DialogTitle>
            <DialogDescription className="text-center">
              {t('claim.wonDesc')}
            </DialogDescription>
          </DialogHeader>

          {got && (
            <div className="space-y-3">
              {/* 额度：最显眼的位置 */}
              <div className="rounded-2xl bg-muted px-4 py-5 text-center">
                <div className="text-3xl font-semibold tabular-nums text-amber-600 dark:text-amber-400">
                  {amountText(got.amount, got.quota_kind)}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {got.quota_kind === 'token' ? t('claim.unitTokens') : t('claim.unitCredits')}
                </div>
              </div>

              {/* 密钥 + 复制 */}
              <div className="rounded-2xl bg-muted px-3 py-3">
                <div className="mb-1.5 text-[11px] text-muted-foreground">
                  {t('claim.yourKey')}
                </div>
                <div className="break-all rounded-xl bg-background px-3 py-2 font-mono text-[11px] leading-5">
                  {got.key}
                </div>
                <Button
                  variant="outline"
                  className="mt-2 w-full rounded-full"
                  onClick={copyKey}
                >
                  <Copy className="mr-1.5 h-4 w-4" />{t('claim.copy')}
                </Button>
                {got.models.length > 0 && (
                  <div className="mt-1.5 text-[10px] leading-4 text-muted-foreground">
                    {t('redPacket.modelsScoped', {list: got.models.join(', ')})}
                  </div>
                )}
              </div>

              <Button className="w-full rounded-full" onClick={closeAndCopy}>
                <Check className="mr-1.5 h-4 w-4" />{t('claim.done')}
              </Button>
              <p className="text-center text-[10px] leading-4 text-muted-foreground">
                {t('claim.closeHint')}
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
