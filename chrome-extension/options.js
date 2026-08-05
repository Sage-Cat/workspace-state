const defaults = {profile: 'Default', appId: 'google-chrome'};

async function load() {
    const config = {...defaults, ...await chrome.storage.local.get(defaults)};
    document.querySelector('#profile').value = config.profile;
    document.querySelector('#appId').value = config.appId;
}

async function save() {
    const profile = document.querySelector('#profile').value.trim() || defaults.profile;
    const appId = document.querySelector('#appId').value.trim() || defaults.appId;
    await chrome.storage.local.set({profile, appId});
    const status = document.querySelector('#status');
    status.textContent = 'Saved';
    setTimeout(() => { status.textContent = ''; }, 1500);
}

document.querySelector('#save').addEventListener('click', save);
load();
