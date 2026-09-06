import js from '@eslint/js'
import prettier from 'eslint-config-prettier'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  // Generated API clients are never hand-edited, so never linted either.
  { ignores: ['dist', 'node_modules', 'coverage', 'src/api/generated'] },
  js.configs.recommended,

  {
    files: ['**/*.{ts,tsx}'],
    // Type-aware rules only where a tsconfig project actually covers the file.
    extends: [tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // Auth tokens live in memory only (AGENTS.md §6). Make a slip loud.
      // Both spellings are covered: the bare global and the window property,
      // because banning only the global leaves `window.localStorage` open.
      'no-restricted-globals': [
        'error',
        { name: 'localStorage', message: 'Never store auth state in localStorage.' },
        { name: 'sessionStorage', message: 'Never store auth state in sessionStorage.' },
      ],
      'no-restricted-properties': [
        'error',
        {
          object: 'window',
          property: 'localStorage',
          message:
            'Browser storage is off-limits for auth state. Non-credential preferences may ' +
            'opt out with an explicit eslint-disable and a comment saying why.',
        },
        {
          object: 'window',
          property: 'sessionStorage',
          message: 'Browser storage is off-limits for auth state.',
        },
      ],
    },
  },

  // v7 exposes the flat-config variants under `.flat`; the top-level keys are
  // still the legacy eslintrc shape.
  reactHooks.configs.flat['recommended-latest'],
  reactRefresh.configs.vite,

  {
    files: ['**/*.js'],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: globals.node },
  },

  // Must stay last: turns off every rule Prettier already decides.
  prettier,
)
