const parameters = new URLSearchParams(location.search);
const token = parameters.get('token') ?? '';
const mode = parameters.get('mode') === 'create' ? 'create' : 'identify';
document.title = `wsctl-${mode}:${token}`;
