"""Create runtime-only model clients and validate portable configuration."""
import re
from urllib.parse import urlsplit

def validate_config(config, model_overrides=None):
    """Validate a non-secret model configuration, shared by both interfaces."""
    if not isinstance(config, dict) or set(config) - {'model', 'limits'}:
        raise ValueError('Configuration supports only model and limits sections')
    model = config.setdefault('model', {})
    limits = config.setdefault('limits', {})
    if not isinstance(model, dict) or not isinstance(limits, dict):
        raise ValueError('model and limits must be mappings')
    if set(model) - {'name', 'base_url', 'api', 'api_key_env'}:
        raise ValueError('Unsupported model field; never put API keys in YAML')
    allowed = {'input_tokens', 'output_tokens', 'steps', 'seconds', 'tool_timeout'}
    if set(limits) - allowed:
        raise ValueError('Unsupported limits field')
    for field, value in (model_overrides or {}).items():
        if value is not None:
            model[field] = value
    if not isinstance(model.get('name'), str) or not model['name'].strip():
        raise ValueError('model.name is required')
    url = urlsplit(model.get('base_url', ''))
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('model.base_url must be an HTTP(S) URL without credentials/query/fragment')
    model.setdefault('api', 'chat_completions')
    model.setdefault('api_key_env', 'MENDPILOT_API_KEY')
    if not isinstance(model['api_key_env'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', model['api_key_env']):
        raise ValueError('model.api_key_env must be an environment variable name')
    if model['api'] not in {'chat_completions', 'responses'}:
        raise ValueError('model.api must be chat_completions or responses')
    defaults = dict(input_tokens=24000, output_tokens=8192, steps=30, seconds=900, tool_timeout=60)
    for key, default in defaults.items():
        limits.setdefault(key, default)
        if type(limits[key]) is not int or limits[key] <= 0:
            raise ValueError('All limits must be positive integers')
    if limits['input_tokens'] <= 8192 or limits['steps'] < 8:
        raise ValueError('input_tokens must exceed 8192; steps must be at least 8')
    return config

def build_model(config, credential, deadline, *, templates=None):
    from mendpilot.models.responses import ResponseContextWindow, WindowedResponseModel
    from mendpilot.models.chat import ChatModel
    window = ResponseContextWindow(config['limits']['input_tokens'])
    options = {'api_base': config['model']['base_url'], 'timeout': 180}
    response_api = config['model']['api'] == 'responses'
    options['max_output_tokens' if response_api else 'max_tokens'] = config['limits']['output_tokens']
    if response_api:
        options['store'] = False
    cls = WindowedResponseModel if response_api else ChatModel
    model = cls(runtime_credential=credential, window=window,
                model_name='openai/' + config['model']['name'].removeprefix('openai/'),
                cost_tracking='ignore_errors', model_kwargs=options,
                **({'observation_template': '{{ output.output }}'} | (templates or {})))
    model.deadline = deadline
    return model, window
