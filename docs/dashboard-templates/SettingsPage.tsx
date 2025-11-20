import { useState, useEffect } from 'react';

interface LLMConfig {
  provider: 'openai' | 'anthropic' | 'fireworks';
  model?: string;
  temperature?: number;
  max_tokens?: number;
}

interface Settings {
  llm: LLMConfig;
  apiUrl: string;
  darkMode: boolean;
}

export function SettingsPage() {
  const [settings, setSettings] = useState<Settings>({
    llm: {
      provider: 'openai',
      model: 'gpt-4',
      temperature: 0.7,
      max_tokens: 2000,
    },
    apiUrl: import.meta.env.VITE_API_URL || 'http://localhost:8000',
    darkMode: false,
  });

  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [testingConnection, setTestingConnection] = useState(false);

  useEffect(() => {
    // Load settings from localStorage
    const savedSettings = localStorage.getItem('faultmaven_settings');
    if (savedSettings) {
      try {
        const parsed = JSON.parse(savedSettings);
        setSettings(prev => ({ ...prev, ...parsed }));
      } catch (err) {
        console.error('Failed to parse saved settings:', err);
      }
    }
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setSaveMessage(null);

    try {
      // Save to localStorage
      localStorage.setItem('faultmaven_settings', JSON.stringify(settings));

      // Optionally sync with backend
      const token = localStorage.getItem('auth_token');
      if (token) {
        try {
          const response = await fetch(`${settings.apiUrl}/v1/settings`, {
            method: 'PUT',
            headers: {
              'Authorization': `Bearer ${token}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(settings),
          });
          if (!response.ok && response.status !== 404) {
            console.warn('Could not sync settings with backend:', response.statusText);
          }
        } catch (err) {
          console.warn('Could not sync settings with backend:', err);
        }
      }

      setSaveMessage({ type: 'success', text: 'Settings saved successfully!' });
    } catch (err) {
      setSaveMessage({ type: 'error', text: 'Failed to save settings. Please try again.' });
    } finally {
      setSaving(false);
      // Clear message after 3 seconds
      setTimeout(() => setSaveMessage(null), 3000);
    }
  };

  const testConnection = async () => {
    setTestingConnection(true);
    setSaveMessage(null);

    try {
      const response = await fetch(`${settings.apiUrl}/health`);
      if (response.ok) {
        setSaveMessage({ type: 'success', text: 'Connection successful!' });
      } else {
        setSaveMessage({ type: 'error', text: `Connection failed: ${response.statusText}` });
      }
    } catch (err) {
      setSaveMessage({ type: 'error', text: 'Cannot connect to API. Please check the URL.' });
    } finally {
      setTestingConnection(false);
      setTimeout(() => setSaveMessage(null), 5000);
    }
  };

  const modelOptions = {
    openai: [
      { value: 'gpt-4', label: 'GPT-4' },
      { value: 'gpt-4-turbo', label: 'GPT-4 Turbo' },
      { value: 'gpt-3.5-turbo', label: 'GPT-3.5 Turbo' },
    ],
    anthropic: [
      { value: 'claude-3-opus', label: 'Claude 3 Opus' },
      { value: 'claude-3-sonnet', label: 'Claude 3 Sonnet' },
      { value: 'claude-3-haiku', label: 'Claude 3 Haiku' },
    ],
    fireworks: [
      { value: 'mixtral-8x7b', label: 'Mixtral 8x7B' },
      { value: 'llama-2-70b', label: 'Llama 2 70B' },
    ],
  };

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white shadow">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
          <h1 className="text-3xl font-bold text-gray-900">Settings</h1>
          <p className="mt-1 text-sm text-gray-600">
            Configure your FaultMaven dashboard and LLM preferences
          </p>
        </div>
      </header>

      {/* Settings Form */}
      <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Save Message */}
        {saveMessage && (
          <div className={`rounded-md p-4 mb-6 ${saveMessage.type === 'success' ? 'bg-green-50' : 'bg-red-50'}`}>
            <div className="flex">
              <div className="flex-shrink-0">
                {saveMessage.type === 'success' ? (
                  <svg className="h-5 w-5 text-green-400" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
                  </svg>
                ) : (
                  <svg className="h-5 w-5 text-red-400" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                  </svg>
                )}
              </div>
              <div className="ml-3">
                <p className={`text-sm font-medium ${saveMessage.type === 'success' ? 'text-green-800' : 'text-red-800'}`}>
                  {saveMessage.text}
                </p>
              </div>
            </div>
          </div>
        )}

        {/* API Configuration */}
        <div className="bg-white shadow rounded-lg mb-6">
          <div className="px-6 py-5 border-b border-gray-200">
            <h2 className="text-lg font-medium text-gray-900">API Configuration</h2>
            <p className="mt-1 text-sm text-gray-600">
              Configure your FaultMaven API endpoint
            </p>
          </div>
          <div className="px-6 py-5 space-y-4">
            <div>
              <label htmlFor="apiUrl" className="block text-sm font-medium text-gray-700">
                API URL
              </label>
              <div className="mt-1 flex rounded-md shadow-sm">
                <input
                  type="url"
                  name="apiUrl"
                  id="apiUrl"
                  value={settings.apiUrl}
                  onChange={(e) => setSettings({ ...settings, apiUrl: e.target.value })}
                  className="flex-1 min-w-0 block w-full px-3 py-2 rounded-md border border-gray-300 focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                  placeholder="http://localhost:8000"
                />
                <button
                  type="button"
                  onClick={testConnection}
                  disabled={testingConnection}
                  className="ml-3 inline-flex items-center px-4 py-2 border border-gray-300 shadow-sm text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50"
                >
                  {testingConnection ? (
                    <>
                      <svg className="animate-spin -ml-1 mr-2 h-4 w-4 text-gray-700" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Testing...
                    </>
                  ) : (
                    'Test Connection'
                  )}
                </button>
              </div>
              <p className="mt-2 text-sm text-gray-500">
                For self-hosted: http://localhost:8000 | For enterprise: https://api.faultmaven.ai
              </p>
            </div>
          </div>
        </div>

        {/* LLM Configuration */}
        <div className="bg-white shadow rounded-lg mb-6">
          <div className="px-6 py-5 border-b border-gray-200">
            <h2 className="text-lg font-medium text-gray-900">LLM Configuration</h2>
            <p className="mt-1 text-sm text-gray-600">
              Configure AI model preferences for troubleshooting assistance
            </p>
          </div>
          <div className="px-6 py-5 space-y-6">
            {/* Provider */}
            <div>
              <label htmlFor="provider" className="block text-sm font-medium text-gray-700">
                LLM Provider
              </label>
              <select
                id="provider"
                value={settings.llm.provider}
                onChange={(e) => {
                  const provider = e.target.value as 'openai' | 'anthropic' | 'fireworks';
                  setSettings({
                    ...settings,
                    llm: {
                      ...settings.llm,
                      provider,
                      model: modelOptions[provider][0].value,
                    },
                  });
                }}
                className="mt-1 block w-full pl-3 pr-10 py-2 text-base border-gray-300 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm rounded-md"
              >
                <option value="openai">OpenAI (GPT-4, GPT-3.5)</option>
                <option value="anthropic">Anthropic (Claude)</option>
                <option value="fireworks">Fireworks AI (Open Models)</option>
              </select>
            </div>

            {/* Model */}
            <div>
              <label htmlFor="model" className="block text-sm font-medium text-gray-700">
                Model
              </label>
              <select
                id="model"
                value={settings.llm.model}
                onChange={(e) => setSettings({
                  ...settings,
                  llm: { ...settings.llm, model: e.target.value },
                })}
                className="mt-1 block w-full pl-3 pr-10 py-2 text-base border-gray-300 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm rounded-md"
              >
                {modelOptions[settings.llm.provider].map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Temperature */}
            <div>
              <label htmlFor="temperature" className="block text-sm font-medium text-gray-700">
                Temperature: {settings.llm.temperature?.toFixed(2)}
              </label>
              <input
                type="range"
                id="temperature"
                min="0"
                max="1"
                step="0.05"
                value={settings.llm.temperature}
                onChange={(e) => {
                  const temp = parseFloat(e.target.value);
                  if (!isNaN(temp) && temp >= 0 && temp <= 1) {
                    setSettings({
                      ...settings,
                      llm: { ...settings.llm, temperature: temp },
                    });
                  }
                }}
                className="mt-1 block w-full"
              />
              <div className="flex justify-between text-xs text-gray-500 mt-1">
                <span>Focused (0.0)</span>
                <span>Creative (1.0)</span>
              </div>
              <p className="mt-2 text-sm text-gray-500">
                Lower values make output more focused and deterministic. Higher values increase creativity.
              </p>
            </div>

            {/* Max Tokens */}
            <div>
              <label htmlFor="max_tokens" className="block text-sm font-medium text-gray-700">
                Max Tokens
              </label>
              <input
                type="number"
                id="max_tokens"
                min="100"
                max="8000"
                step="100"
                value={settings.llm.max_tokens}
                onChange={(e) => {
                  const tokens = parseInt(e.target.value);
                  if (!isNaN(tokens) && tokens >= 100 && tokens <= 8000) {
                    setSettings({
                      ...settings,
                      llm: { ...settings.llm, max_tokens: tokens },
                    });
                  }
                }}
                className="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
              />
              <p className="mt-2 text-sm text-gray-500">
                Maximum length of AI responses. Higher values allow longer responses but cost more.
              </p>
            </div>

            {/* API Key Info */}
            <div className="rounded-md bg-yellow-50 p-4">
              <div className="flex">
                <div className="flex-shrink-0">
                  <svg className="h-5 w-5 text-yellow-400" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
                  </svg>
                </div>
                <div className="ml-3">
                  <h3 className="text-sm font-medium text-yellow-800">
                    API Key Configuration
                  </h3>
                  <div className="mt-2 text-sm text-yellow-700">
                    <p>
                      LLM API keys are configured on the backend server via environment variables.
                      To change your API key, update the <code className="text-xs bg-yellow-100 px-1 py-0.5 rounded">.env</code> file
                      in your FaultMaven deployment and restart services.
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Display Preferences */}
        <div className="bg-white shadow rounded-lg mb-6">
          <div className="px-6 py-5 border-b border-gray-200">
            <h2 className="text-lg font-medium text-gray-900">Display Preferences</h2>
            <p className="mt-1 text-sm text-gray-600">
              Customize how the dashboard looks and feels
            </p>
          </div>
          <div className="px-6 py-5 space-y-4">
            {/* Dark Mode */}
            <div className="flex items-center justify-between">
              <div>
                <label htmlFor="darkMode" className="text-sm font-medium text-gray-700">
                  Dark Mode
                </label>
                <p className="text-sm text-gray-500">
                  Enable dark theme for the dashboard
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={settings.darkMode}
                onClick={() => setSettings({ ...settings, darkMode: !settings.darkMode })}
                className={`${
                  settings.darkMode ? 'bg-blue-600' : 'bg-gray-200'
                } relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2`}
              >
                <span
                  className={`${
                    settings.darkMode ? 'translate-x-5' : 'translate-x-0'
                  } pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out`}
                />
              </button>
            </div>

            <div className="rounded-md bg-blue-50 p-4">
              <div className="flex">
                <div className="flex-shrink-0">
                  <svg className="h-5 w-5 text-blue-400" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" />
                  </svg>
                </div>
                <div className="ml-3">
                  <p className="text-sm text-blue-700">
                    Dark mode is currently in development. This setting will be applied in a future update.
                  </p>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Save Button */}
        <div className="flex justify-end">
          <button
            type="button"
            onClick={handleSave}
            disabled={saving}
            className="inline-flex items-center px-6 py-3 border border-transparent text-base font-medium rounded-md shadow-sm text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50"
          >
            {saving ? (
              <>
                <svg className="animate-spin -ml-1 mr-3 h-5 w-5 text-white" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Saving...
              </>
            ) : (
              <>
                <svg className="h-5 w-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                Save Settings
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

export default SettingsPage;
