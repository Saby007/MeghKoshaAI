if (!window.opener && window.parent === window) {
  void import('./apiIdentity').then(({ completeIdentityRedirect }) => completeIdentityRedirect()).catch(() => {
    const status = document.getElementById('identity-status');
    if (status) status.textContent = 'Microsoft sign-in could not complete. Return to the app and try again.';
    document.getElementById('identity-retry')?.removeAttribute('hidden');
  });
}