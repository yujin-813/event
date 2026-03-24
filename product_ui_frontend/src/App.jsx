import React, { useEffect, useMemo, useState } from 'react'

const MENUS = ['Project', 'Versions', 'Sessions', 'DefinitionSpecs', 'Results', 'Settings']
const MENU_LABELS = {
  Project: '프로젝트',
  Versions: '버전',
  Sessions: '세션',
  DefinitionSpecs: '정의서 관리',
  Results: '결과',
  Settings: '설정'
}
const RUN_FILTERS = [
  ['all', '전체'],
  ['exploratory', '탐색형'],
  ['scenario', '시나리오'],
  ['spec_validation', '정의서 검증']
]
const SCOPE_OPTIONS = [
  ['site_wide', '전체 사이트 탐색'],
  ['start_page', '특정 시작 페이지'],
  ['scenario_group', '시나리오 페이지군'],
  ['single_url', '단일 URL 검증']
]
const PROJECT_STORAGE_KEY = 'qa_ui_selected_project'
const RUN_FILTER_STORAGE_KEY = 'qa_ui_run_filter'

function toSavedPagesText(list) {
  if (!Array.isArray(list)) return ''
  return list
    .map((row) => `${row?.name || ''}|${row?.url || ''}`.trim())
    .filter(Boolean)
    .join('\n')
}

function toScenarioGroupsText(list) {
  if (!Array.isArray(list)) return ''
  return list
    .map((row) => `${row?.name || ''}|${Array.isArray(row?.urls) ? row.urls.join(', ') : ''}`.trim())
    .filter(Boolean)
    .join('\n')
}

function hydrateSettings(raw) {
  const src = raw || {}
  return {
    base_domain: src.base_domain || '',
    default_start_url: src.default_start_url || '',
    browser: src.browser || 'chromium',
    viewport: src.viewport || 'Desktop 1440x900',
    collection_option: src.collection_option || 'Auto Crawl',
    judgement_rule: src.judgement_rule || 'Network First',
    saved_start_pages: Array.isArray(src.saved_start_pages) ? src.saved_start_pages : [],
    scenario_page_groups: Array.isArray(src.scenario_page_groups) ? src.scenario_page_groups : [],
    saved_start_pages_text: toSavedPagesText(src.saved_start_pages),
    scenario_page_groups_text: toScenarioGroupsText(src.scenario_page_groups)
  }
}

async function parseResponseJsonOrThrow(res) {
  const raw = await res.text()
  let data = null
  try {
    data = raw ? JSON.parse(raw) : null
  } catch (err) {
    throw new Error(raw || `request failed: ${res.status}`)
  }
  return data
}

async function apiGet(url) {
  const res = await fetch(url, { cache: 'no-store' })
  const data = await parseResponseJsonOrThrow(res)
  if (!res.ok || !data?.ok) {
    throw new Error(data?.error || `request failed: ${res.status}`)
  }
  return data
}

async function apiPost(url, payload) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  const data = await parseResponseJsonOrThrow(res)
  if (!res.ok || !data?.ok) {
    throw new Error(data?.error || `request failed: ${res.status}`)
  }
  return data
}

async function apiUploadDefinition(project, file) {
  const fd = new FormData()
  fd.append('project', project)
  fd.append('definition_file', file)
  const res = await fetch('/definitions/upload', { method: 'POST', body: fd })
  const data = await parseResponseJsonOrThrow(res)
  if (!res.ok || !data?.ok) {
    throw new Error(data?.error || `request failed: ${res.status}`)
  }
  return data
}

function StatusPill({ value }) {
  const safe = String(value || 'Unchecked')
  const cls = safe.toLowerCase().replaceAll(' ', '-').replaceAll(':', '')
  return <span className={`status ${cls}`}>{safe}</span>
}

function formatDuration(startedAt, endedAt) {
  const s = startedAt ? new Date(startedAt) : null
  const e = endedAt ? new Date(endedAt) : null
  if (!s || Number.isNaN(s.getTime())) return '-'
  const end = e && !Number.isNaN(e.getTime()) ? e : new Date()
  const sec = Math.max(0, Math.floor((end.getTime() - s.getTime()) / 1000))
  const hh = String(Math.floor(sec / 3600)).padStart(2, '0')
  const mm = String(Math.floor((sec % 3600) / 60)).padStart(2, '0')
  const ss = String(sec % 60).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

function formatKstDateTime(raw) {
  const text = String(raw || '').trim()
  if (!text) return '-'
  const d = new Date(text)
  if (Number.isNaN(d.getTime())) return text
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }).format(d)
}

function runtimeHeadline(runtimeStatus) {
  const v = String(runtimeStatus || '').toLowerCase()
  if (v === 'running' || v === 'stopping') return '세션 실행 중'
  if (v === 'auto_crawl_paused') return '세션 중단'
  if (v) return '세션 완료'
  return '세션 상태 없음'
}

function validationStateLabel(state) {
  const v = String(state || '').toLowerCase()
  if (v === 'completed') return '판정 완료'
  if (v === 'in_progress') return '판정 중...'
  if (v === 'unavailable') return '판정 비대상'
  return '판정 대기'
}

function validationStateClass(state) {
  const v = String(state || '').toLowerCase()
  if (v === 'completed') return 'matched'
  if (v === 'in_progress') return 'unchecked'
  if (v === 'unavailable') return 'blocked'
  return 'unchecked'
}

function isSystemEventName(name) {
  const text = String(name || '').trim().toLowerCase()
  if (!text) return true
  if (text.includes('max_run_minutes_reached')) return true
  if (text.startsWith('manual_flow')) return true
  if (text.startsWith('qa_')) return true
  if (text.startsWith('auto_crawl_')) return true
  return false
}

function extractRunIdFromExportPath(path) {
  const parts = String(path || '').split('/').filter(Boolean)
  if (parts.length < 2) return ''
  return parts[parts.length - 2] || ''
}

function extractFileNameFromExportPath(path) {
  const parts = String(path || '').split('/').filter(Boolean)
  return parts.length > 0 ? parts[parts.length - 1] : ''
}

function exportLabel(filename) {
  const f = String(filename || '').trim().toLowerCase()
  if (f === 'qa_result.csv') return '빠른 결과 CSV (즉시)'
  if (f === 'qa_review.xlsx') return 'QA 리뷰 파일 (기본)'
  if (f === 'qa_result.xlsx') return 'QA 리뷰 파일 (호환)'
  if (f === 'qa_issues_only.xlsx') return '문제 항목 전용 (선택)'
  if (f === 'qa_raw_debug.xlsx') return 'Raw / Debug (고급)'
  return filename || '-'
}

function deriveQaPolicy(qaMode) {
  const mode = String(qaMode || '').trim()
  if (mode === '시나리오 테스트') {
    return {
      run_type: 'scenario',
      run_type_label: 'Scenario',
      test_scope: 'scenario_group',
      test_scope_label: '시나리오 페이지군',
      start_mode: 'manual'
    }
  }
  if (mode === '정의서 검증') {
    return {
      run_type: 'spec_validation',
      run_type_label: 'Spec Validation',
      test_scope: 'single_url',
      test_scope_label: '단일 URL 검증',
      start_mode: 'manual'
    }
  }
  return {
    run_type: 'exploratory',
    run_type_label: 'Exploratory',
    test_scope: 'site_wide',
    test_scope_label: '전체 사이트 탐색',
    start_mode: 'auto'
  }
}

function extractPathFromUrl(rawUrl) {
  const text = String(rawUrl || '').trim()
  if (!text) return ''
  try {
    const u = new URL(text)
    const path = u.pathname || '/'
    const queryCount = Array.from(u.searchParams.keys()).length
    if (queryCount > 0) {
      return `${path} · query ${queryCount}개`
    }
    return path
  } catch (_err) {
    return text
  }
}

function buildRunTitle(session) {
  const s = session || {}
  const scope = String(s.test_scope || '')
  if (scope === 'site_wide') return '전체 사이트 탐색'
  if (scope === 'single_url') return '단일 URL 검증'
  if (scope === 'scenario_group') return '시나리오 테스트'
  if (String(s.qa_mode || '') === '정의서 검증') return '정의서 검증'
  return String(s.qa_mode || '테스트').trim() || '테스트'
}

function buildRunSubline(session) {
  const s = session || {}
  const scope = String(s.test_scope || '').trim() || 'start_page'
  let context = ''
  if (scope === 'scenario_group') {
    context = String(s.scenario_group_name || '').trim()
  } else if (scope === 'start_page') {
    context = String(s.saved_page_name || '').trim() || extractPathFromUrl(s.target_url)
  } else if (scope === 'single_url') {
    context = extractPathFromUrl(s.target_url)
  } else if (scope === 'site_wide') {
    context = extractPathFromUrl(s.target_url)
  }
  if (!context && String(s.qa_mode || '') === '정의서 검증') {
    context = String(s.definition_spec_name || '').trim()
  }
  return context ? `${context} · ${scope}` : scope
}

function buildQaModeStatusLine(session) {
  const s = session || {}
  const runtime = String(s.runtime_status || '').trim().toLowerCase()
  const hits = Number(s.captured_events || 0)
  if (runtime === 'running') return `진행중 · 이벤트 ${hits} hits 수집`
  if (runtime === 'stopping') return `중지 처리중 · 이벤트 ${hits} hits 수집`
  if (runtime === 'auto_crawl_paused') return `일시중지 · 이벤트 ${hits} hits 수집`
  if (runtime === 'stopped' || runtime === 'completed') return `종료 · 이벤트 ${hits} hits 수집`
  if (hits > 0) return `이벤트 ${hits} hits 수집`
  return '수집 대기'
}

function buildStartModeStatusLine(session, startMode, summary = null) {
  const mode = String(startMode || 'manual').trim().toLowerCase()
  const s = session || {}
  const runtime = String(s.runtime_status || '').trim().toLowerCase()
  const hits = Number(s.captured_events || 0)
  const strictMatched = Number(summary?.definition_match_counts?.strict_matched || summary?.validation_counts?.Matched || 0)
  const relaxedMatched = Number(summary?.definition_match_counts?.relaxed_matched || 0)
  const thirdMatched = Number(summary?.definition_match_counts?.third_matched || 0)
  const totalMatched = Number(summary?.definition_match_counts?.total_matched || (strictMatched + relaxedMatched + thirdMatched))
  const matchText = `매칭 ${totalMatched}건 (1차 ${strictMatched} / 2차 ${relaxedMatched} / 3차 ${thirdMatched})`

  if (!session) {
    return mode === 'auto' ? '현재 상태: 자동수집 대기' : '현재 상태: 수동 대기'
  }
  if (runtime === 'running') {
    return mode === 'auto'
      ? `현재 상태: 자동수집 진행중 (이벤트 ${hits} hits · ${matchText})`
      : `현재 상태: 수동 진행중 (이벤트 ${hits} hits · ${matchText})`
  }
  if (runtime === 'stopping') {
    return `현재 상태: 중지 처리중 (이벤트 ${hits} hits · ${matchText})`
  }
  if (runtime === 'auto_crawl_paused') {
    return `현재 상태: 자동수집 일시중지 (이벤트 ${hits} hits · ${matchText})`
  }
  if (runtime === 'stopped' || runtime === 'completed') {
    return `현재 상태: 최근 실행 종료 (이벤트 ${hits} hits · ${matchText})`
  }
  return mode === 'auto' ? '현재 상태: 자동수집 대기' : '현재 상태: 수동 대기'
}

function analyticsSourceText(summary, session) {
  const s = summary || {}
  const sess = session || {}
  const label = String(s.analytics_source_label || sess.analytics_source_label || 'Unknown').trim() || 'Unknown'
  const ga4 = Number(s.ga4_hits ?? sess.ga4_hits ?? 0)
  const amp = Number(s.amplitude_hits ?? sess.amplitude_hits ?? 0)
  if (label === 'GA4+Amplitude') return `데이터 소스: GA4+Amplitude (GA4 ${ga4} / AMP ${amp})`
  if (label === 'GA4') return `데이터 소스: GA4 (hits ${ga4})`
  if (label === 'Amplitude') return `데이터 소스: Amplitude (hits ${amp})`
  return '데이터 소스: Unknown'
}

export function App() {
  const [project, setProject] = useState(() => {
    try {
      return window.localStorage.getItem(PROJECT_STORAGE_KEY) || ''
    } catch (_) {
      return ''
    }
  })
  const [menu, setMenu] = useState('Sessions')
  const [runFilter, setRunFilter] = useState(() => {
    try {
      return window.localStorage.getItem(RUN_FILTER_STORAGE_KEY) || 'all'
    } catch (_) {
      return 'all'
    }
  })
  const [loading, setLoading] = useState(true)
  const [resultsLoading, setResultsLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [newProjectName, setNewProjectName] = useState('')
  const [newProjectDomain, setNewProjectDomain] = useState('')

  const [projects, setProjects] = useState([])
  const [overview, setOverview] = useState({})
  const [versions, setVersions] = useState([])
  const [deletedVersions, setDeletedVersions] = useState([])
  const [definitionSpecs, setDefinitionSpecs] = useState([])
  const [results, setResults] = useState({ status_counts: {}, destination_rate: {}, exports: [], collection_notices: [] })
  const [settings, setSettings] = useState(hydrateSettings({}))

  const [sessions, setSessions] = useState([])
  const [sessionDetail, setSessionDetail] = useState({ session_id: '', summary: {}, suspicion: [], events: [] })
  const [modeStatusDetail, setModeStatusDetail] = useState({ session_id: '', summary: {}, suspicion: [], events: [] })
  const [selectedSessionId, setSelectedSessionId] = useState('')
  const [showExportFiles, setShowExportFiles] = useState(false)
  const [showAdvancedExports, setShowAdvancedExports] = useState(false)

  const [startForm, setStartForm] = useState({
    version_id: '',
    run_type: 'exploratory',
    qa_mode: '전체 탐색 테스트',
    tester_name: '',
    start_mode: 'manual',
    analytics_source_mode: 'both',
    viewport: 'Desktop 1440x900',
    test_scope: 'site_wide',
    start_page_mode: 'use_default',
    start_url: '',
    saved_page_name: '',
    scenario_group_name: '',
    definition_spec_id: ''
  })
  const [definitionUploadFile, setDefinitionUploadFile] = useState(null)
  const [selectedDefinitionSpecId, setSelectedDefinitionSpecId] = useState('')

  const selectedProjectName = useMemo(() => {
    const item = projects.find((p) => p.slug === project)
    return item ? item.name : '-'
  }, [projects, project])

  const versionNameById = useMemo(() => {
    const map = {}
    for (const v of versions) {
      map[v.id] = v.display_name || v.id
    }
    return map
  }, [versions])

  const selectedDefinitionSpec = useMemo(
    () => (definitionSpecs || []).find((x) => x.id === selectedDefinitionSpecId) || null,
    [definitionSpecs, selectedDefinitionSpecId]
  )
  const qaPolicy = useMemo(() => deriveQaPolicy(startForm.qa_mode), [startForm.qa_mode])

  const latestSession = useMemo(() => (sessions && sessions.length > 0 ? sessions[0] : null), [sessions])
  const modeStatusSession = useMemo(() => {
    const running = (sessions || []).find((s) => {
      const runtime = String(s?.runtime_status || '').trim().toLowerCase()
      return runtime === 'running' || runtime === 'stopping'
    })
    if (running) return running
    const selected = (sessions || []).find((s) => s.session_id === selectedSessionId)
    if (selected) return selected
    return latestSession
  }, [sessions, selectedSessionId, latestSession])
  const modeStatusSummary = useMemo(() => {
    const sid = String(modeStatusSession?.session_id || '').trim()
    if (!sid) return null
    if (String(sessionDetail?.summary?.session_id || '').trim() === sid) {
      return sessionDetail.summary || null
    }
    if (String(modeStatusDetail?.summary?.session_id || '').trim() === sid) {
      return modeStatusDetail.summary || null
    }
    return null
  }, [modeStatusSession, sessionDetail, modeStatusDetail])

  const loadBootstrap = async (targetProject = project, targetFilter = runFilter) => {
    setLoading(true)
    setError('')
    try {
      const data = await apiGet(`/api/bootstrap?project=${encodeURIComponent(targetProject || '')}&run_filter=${encodeURIComponent(targetFilter)}`)
      setProjects(data.projects || [])
      const resolvedProject = data.selected_project || targetProject
      setProject(resolvedProject || '')
      try {
        if (resolvedProject) window.localStorage.setItem(PROJECT_STORAGE_KEY, resolvedProject)
      } catch (_) {
        // no-op
      }
      setOverview(data.overview || {})
      const nextVersions = data.versions || []
      setVersions(nextVersions)
      setDeletedVersions(data.deleted_versions || [])
      const nextSpecs = data.definition_specs || []
      setDefinitionSpecs(nextSpecs)
      setResults(data.results || { status_counts: {}, destination_rate: {}, exports: [], collection_notices: [] })

      const hydrated = hydrateSettings(data.settings || {})
      setSettings(hydrated)

      setStartForm((prev) => {
        const hasSelectedSpec = !!nextSpecs.find((x) => x.id === prev.definition_spec_id)
        return {
          ...prev,
          version_id: prev.version_id || (nextVersions[0]?.id || ''),
          viewport: prev.viewport || (hydrated.viewport || 'Desktop 1440x900'),
          saved_page_name: prev.saved_page_name || (hydrated.saved_start_pages[0]?.name || ''),
          scenario_group_name: prev.scenario_group_name || (hydrated.scenario_page_groups[0]?.name || ''),
          definition_spec_id: hasSelectedSpec ? prev.definition_spec_id : (nextSpecs[0]?.id || '')
        }
      })
      setSelectedDefinitionSpecId((prev) => {
        const hasSelectedSpec = !!nextSpecs.find((x) => x.id === prev)
        return hasSelectedSpec ? prev : (nextSpecs[0]?.id || '')
      })
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setLoading(false)
    }
  }

  const loadSessions = async (targetProject = project, targetFilter = runFilter) => {
    if (!targetProject) return
    try {
      const data = await apiGet(`/api/sessions?project=${encodeURIComponent(targetProject)}&run_filter=${encodeURIComponent(targetFilter)}`)
      setSessions(data.sessions || [])
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const loadResultsOnly = async (targetProject = project) => {
    if (!targetProject) return
    const data = await apiGet(`/api/results?project=${encodeURIComponent(targetProject)}`)
    setResults(data.results || { status_counts: {}, destination_rate: {}, exports: [], collection_notices: [] })
  }

  const loadSessionDetail = async (sid) => {
    if (!sid || !project) {
      setSessionDetail({ session_id: '', summary: {}, suspicion: [], events: [] })
      return
    }
    try {
      const data = await apiGet(`/api/session_detail?project=${encodeURIComponent(project)}&session_id=${encodeURIComponent(sid)}`)
      setSessionDetail(data)
      setSelectedSessionId(sid)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const loadModeStatusDetail = async (sid) => {
    if (!sid || !project) {
      setModeStatusDetail({ session_id: '', summary: {}, suspicion: [], events: [] })
      return
    }
    try {
      const data = await apiGet(`/api/session_detail?project=${encodeURIComponent(project)}&session_id=${encodeURIComponent(sid)}`)
      setModeStatusDetail(data)
    } catch (_) {
      // no-op
    }
  }

  useEffect(() => {
    loadBootstrap(project, runFilter)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    try {
      if (project) window.localStorage.setItem(PROJECT_STORAGE_KEY, project)
    } catch (_) {
      // no-op
    }
  }, [project])

  useEffect(() => {
    try {
      if (runFilter) window.localStorage.setItem(RUN_FILTER_STORAGE_KEY, runFilter)
    } catch (_) {
      // no-op
    }
  }, [runFilter])

  useEffect(() => {
    if (project) {
      loadSessions(project, runFilter)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project, runFilter])

  useEffect(() => {
    if (qaPolicy.test_scope !== 'scenario_group') return
    if (startForm.scenario_group_name) return
    const firstName = String(settings?.scenario_page_groups?.[0]?.name || '').trim()
    if (!firstName) return
    setStartForm((prev) => ({ ...prev, scenario_group_name: firstName }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qaPolicy.test_scope, settings?.scenario_page_groups?.length, startForm.scenario_group_name])

  useEffect(() => {
    if (menu !== 'Sessions' || !project) return
    const timer = setInterval(() => {
      loadSessions(project, runFilter)
      if (selectedSessionId) {
        loadSessionDetail(selectedSessionId)
      }
    }, 5000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [menu, project, runFilter, selectedSessionId])

  useEffect(() => {
    if (menu !== 'Sessions' || !project) return
    const sid = String(modeStatusSession?.session_id || '').trim()
    if (!sid) {
      setModeStatusDetail({ session_id: '', summary: {}, suspicion: [], events: [] })
      return
    }
    loadModeStatusDetail(sid)
    const timer = setInterval(() => {
      loadModeStatusDetail(sid)
    }, 5000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [menu, project, modeStatusSession?.session_id])

  useEffect(() => {
    if (menu !== 'Results') return
    if (!project) return
    let mounted = true
    const refreshResults = async () => {
      if (!mounted) return
      setResultsLoading(true)
      try {
        await loadResultsOnly(project)
        await loadSessions(project, runFilter)
        const currentId = selectedSessionId || latestSession?.session_id
        if (currentId) {
          await loadSessionDetail(currentId)
        }
      } catch (err) {
        setError(String(err?.message || err))
      } finally {
        if (mounted) setResultsLoading(false)
      }
    }
    refreshResults()
    const timer = setInterval(() => {
      refreshResults()
    }, 5000)
    return () => {
      mounted = false
      clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [menu, latestSession?.session_id, selectedSessionId, project, runFilter])

  const onProjectChange = async (slug) => {
    setProject(slug)
    await loadBootstrap(slug, runFilter)
    if (menu === 'Sessions') {
      await loadSessions(slug, runFilter)
    }
  }

  const onCreateProject = async () => {
    const name = newProjectName.trim()
    if (!name) {
      setError('프로젝트 이름을 입력해 주세요.')
      return
    }
    try {
      const data = await apiPost('/projects/create', { name, domain: newProjectDomain.trim() })
      setNotice(`프로젝트 생성 완료: ${data?.project?.name || name}`)
      setNewProjectName('')
      setNewProjectDomain('')
      await loadBootstrap(data?.project?.slug || '', runFilter)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const onVersionAction = async (version, action = 'save') => {
    try {
      await apiPost('/versions/save', {
        project,
        version_id: version.id,
        display_name: version.display_name || '',
        status: version.status || 'Draft',
        definition_link: version.definition_link || '',
        change_reason: version.change_reason || '',
        site_change_memo: version.site_change_memo || '',
        action
      })
      setNotice(`버전 ${version.display_name || version.id}: ${action} 완료`)
      await loadBootstrap(project, runFilter)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const onSaveSettings = async () => {
    try {
      await apiPost('/settings/save', {
        project,
        base_domain: settings.base_domain || '',
        default_start_url: settings.default_start_url || '',
        browser: settings.browser || 'chromium',
        viewport: settings.viewport || 'Desktop 1440x900',
        collection_option: settings.collection_option || 'Auto Crawl',
        judgement_rule: settings.judgement_rule || 'Network First',
        saved_start_pages_text: settings.saved_start_pages_text || '',
        scenario_page_groups_text: settings.scenario_page_groups_text || ''
      })
      setNotice('설정 저장 완료')
      await loadBootstrap(project, runFilter)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const onStartSession = async () => {
    try {
      if (startForm.qa_mode === '정의서 검증' && !startForm.definition_spec_id) {
        setError('정의서 검증 모드는 정의서 선택이 필요합니다.')
        return
      }
      if (startForm.qa_mode === '정의서 검증' && !definitionSpecs.find((x) => x.id === startForm.definition_spec_id)) {
        setError(`선택한 정의서를 현재 프로젝트(${project})에서 찾을 수 없습니다. 정의서를 다시 선택해 주세요.`)
        return
      }
      const payload = {
        project,
        run_filter: runFilter,
        ...startForm,
        run_type: qaPolicy.run_type,
        test_scope: qaPolicy.test_scope,
        start_mode: qaPolicy.start_mode
      }
      const data = await apiPost('/sessions/start', payload)
      setNotice(`세션 시작 완료: ${data.session_id}`)
      await loadSessions(project, runFilter)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const onStopSession = async (sid) => {
    try {
      await apiPost('/sessions/stop', {
        project,
        run_filter: runFilter,
        session_id: sid
      })
      setNotice(`세션 종료 요청 완료: ${sid}`)
      await loadSessions(project, runFilter)
      if (selectedSessionId === sid) {
        await loadSessionDetail(sid)
      }
      await loadBootstrap(project, runFilter)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const onDeleteSession = async (sid) => {
    const sessionId = String(sid || '').trim()
    if (!sessionId) return
    if (!window.confirm(`세션을 삭제할까요?\n${sessionId}`)) return
    try {
      await apiPost('/sessions/delete', {
        project,
        run_filter: runFilter,
        session_id: sessionId
      })
      setNotice(`세션 삭제 완료: ${sessionId}`)
      if (selectedSessionId === sessionId) {
        setSelectedSessionId('')
        setSessionDetail({ session_id: '', summary: {}, suspicion: [], events: [] })
      }
      await loadSessions(project, runFilter)
      await loadBootstrap(project, runFilter)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const onUploadDefinition = async () => {
    if (!definitionUploadFile) {
      setError('업로드할 정의서 파일을 선택해 주세요.')
      return
    }
    try {
      const data = await apiUploadDefinition(project, definitionUploadFile)
      setNotice(`정의서 업로드 완료: ${definitionUploadFile.name}`)
      const uploadedSpecId = data?.item?.id ? String(data.item.id) : ''
      if (uploadedSpecId) {
        setStartForm((prev) => ({ ...prev, definition_spec_id: uploadedSpecId }))
        setSelectedDefinitionSpecId(uploadedSpecId)
      }
      setDefinitionUploadFile(null)
      await loadBootstrap(project, runFilter)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const onDeleteDefinition = async (specId) => {
    try {
      await apiPost('/definitions/delete', { project, spec_id: specId })
      setNotice('정의서 삭제 완료')
      await loadBootstrap(project, runFilter)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  const onCopySessionId = async (sid) => {
    const text = String(sid || '').trim()
    if (!text) return
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        setNotice(`session id 복사됨: ${text}`)
      } else {
        setError('이 브라우저에서는 클립보드 복사를 지원하지 않습니다.')
      }
    } catch (err) {
      setError(`복사 실패: ${String(err.message || err)}`)
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <strong>GA4 QA</strong>
          <span>Product Console</span>
        </div>
        <label className="small-label">프로젝트</label>
        <select value={project} onChange={(e) => onProjectChange(e.target.value)}>
          {projects.map((p) => (
            <option key={p.slug} value={p.slug}>{p.name}</option>
          ))}
        </select>

        <nav className="menu">
          {MENUS.map((item) => (
            <button
              key={item}
              type="button"
              className={`menu-item ${menu === item ? 'active' : ''}`}
              onClick={() => setMenu(item)}
            >
              {MENU_LABELS[item] || item}
            </button>
          ))}
        </nav>

        <div className="sidebar-divider" />
        <div className="new-project-box">
          <span className="small-label">프로젝트 생성</span>
          <input placeholder="새 프로젝트 이름" value={newProjectName} onChange={(e) => setNewProjectName(e.target.value)} />
          <input placeholder="기본 도메인(선택)" value={newProjectDomain} onChange={(e) => setNewProjectDomain(e.target.value)} />
          <button type="button" onClick={onCreateProject}>프로젝트 생성</button>
        </div>

        {menu === 'Sessions' && (
          <>
            <div className="small-label mt-16">세션 필터</div>
            <div className="chips">
              {RUN_FILTERS.map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  className={`chip ${runFilter === key ? 'active' : ''}`}
                  onClick={() => setRunFilter(key)}
                >
                  {label}
                </button>
              ))}
            </div>
          </>
        )}
      </aside>

      <main className="main">
        {loading && <div className="alert">로딩 중...</div>}
        {menu === 'Results' && resultsLoading && <div className="alert">결과 집계/파일 확인 중...</div>}
        {notice && <div className="alert success">{notice}</div>}
        {error && <div className="alert error">{error}</div>}

        {menu === 'Project' && (
          <section>
            <h1>프로젝트 개요</h1>
            <p className="muted">{selectedProjectName} 사이트 수집 상태</p>
            <div className="stats-grid">
              <article className="stat-card"><span>최신 버전</span><strong>{overview.latest_version || '-'}</strong></article>
              <article className="stat-card"><span>최근 QA Run 결과</span><strong>{overview.recent_run_result || 'Unchecked'}</strong></article>
              <article className="stat-card"><span>목적지 전송률</span><strong>{overview.destination_success_rate || 0}%</strong></article>
              <article className="stat-card"><span>마지막 테스트</span><strong>{overview.last_tested_at || '-'}</strong></article>
            </div>
          </section>
        )}

        {menu === 'Versions' && (
          <section>
            <h1>버전 관리</h1>
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Version Name</th>
                    <th>생성일</th>
                    <th>상태</th>
                    <th>연결 정의서</th>
                    <th>변경 이유</th>
                    <th>연결 세션 수</th>
                    <th>액션</th>
                  </tr>
                </thead>
                <tbody>
                  {versions.map((v, idx) => (
                    <VersionRow key={v.id} version={v} onAction={onVersionAction} rowIndex={idx} />
                  ))}
                </tbody>
              </table>
            </div>
            {deletedVersions.length > 0 && (
              <div className="card table-wrap">
                <h3>휴지통</h3>
                <table>
                  <thead>
                    <tr>
                      <th>Version Name</th>
                      <th>생성일</th>
                      <th>상태</th>
                      <th>연결 정의서</th>
                      <th>변경 이유</th>
                      <th>연결 세션 수</th>
                      <th>액션</th>
                    </tr>
                  </thead>
                  <tbody>
                    {deletedVersions.map((v, idx) => (
                      <VersionRow key={v.id} version={v} onAction={onVersionAction} rowIndex={idx} isDeleted />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}

        {menu === 'Sessions' && (
          <section>
            <h1>세션 실행</h1>
            <div className="card form-grid run-start-form scope-grid">
              <label>버전
                <select value={startForm.version_id} onChange={(e) => setStartForm((prev) => ({ ...prev, version_id: e.target.value }))}>
                  {versions.map((v) => <option key={v.id} value={v.id}>{v.display_name || v.id}</option>)}
                </select>
              </label>
              <label>QA 모드
                <select value={startForm.qa_mode} onChange={(e) => setStartForm((prev) => ({ ...prev, qa_mode: e.target.value }))}>
                  <option value="전체 탐색 테스트">전체 탐색 테스트</option>
                  <option value="시나리오 테스트">시나리오 테스트</option>
                  <option value="정의서 검증">정의서 검증</option>
                </select>
              </label>
              <label>Run 타입 (자동)
                <input value={qaPolicy.run_type_label} readOnly />
              </label>
              <label>테스트 범위 (자동)
                <input value={qaPolicy.test_scope_label} readOnly />
              </label>
              <label>시작 모드 (자동)
                <input value={qaPolicy.start_mode} readOnly />
                <div className="run-subline muted">{buildStartModeStatusLine(modeStatusSession, qaPolicy.start_mode, modeStatusSummary)}</div>
              </label>
              {startForm.qa_mode === '정의서 검증' && (
                <>
                  <label>검증 정의서
                    <select
                      value={startForm.definition_spec_id}
                      onChange={(e) => setStartForm((prev) => ({ ...prev, definition_spec_id: e.target.value }))}
                    >
                      {(definitionSpecs || []).map((x) => (
                        <option key={x.id} value={x.id}>{x.name} ({x.event_count})</option>
                      ))}
                    </select>
                  </label>
                  <label>정의서 업로드
                    <input type="file" accept=".csv,.xlsx,.xls,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(e) => setDefinitionUploadFile(e.target.files?.[0] || null)} />
                  </label>
                  <div><button type="button" className="neutral-btn" onClick={onUploadDefinition}>업로드</button></div>
                </>
              )}
              <label>Tester
                <input value={startForm.tester_name} onChange={(e) => setStartForm((prev) => ({ ...prev, tester_name: e.target.value }))} />
              </label>
              <label>분석 소스
                <select value={startForm.analytics_source_mode} onChange={(e) => setStartForm((prev) => ({ ...prev, analytics_source_mode: e.target.value }))}>
                  <option value="both">GA4 + Amplitude</option>
                  <option value="ga4">GA4 only</option>
                  <option value="amplitude">Amplitude only</option>
                </select>
              </label>
              <label>뷰포트
                <select value={startForm.viewport} onChange={(e) => setStartForm((prev) => ({ ...prev, viewport: e.target.value }))}>
                  <option value="Desktop 1440x900">Desktop 1440x900</option>
                  <option value="Tablet 768x1024">Tablet 768x1024</option>
                  <option value="Mobile 390x844">Mobile 390x844</option>
                </select>
              </label>
              {qaPolicy.test_scope === 'scenario_group' && (
                <label>시나리오 페이지군
                  <select
                    value={startForm.scenario_group_name}
                    onChange={(e) => setStartForm((prev) => ({ ...prev, scenario_group_name: e.target.value }))}
                  >
                    {(settings.scenario_page_groups || []).map((row) => (
                      <option key={row.name} value={row.name}>{row.name}</option>
                    ))}
                  </select>
                </label>
              )}

              {qaPolicy.test_scope === 'single_url' && (
                <label>검증 URL
                  <input
                    placeholder="https://example.com/page"
                    value={startForm.start_url}
                    onChange={(e) => setStartForm((prev) => ({ ...prev, start_url: e.target.value }))}
                  />
                </label>
              )}

              <div><button type="button" onClick={onStartSession}>테스트 시작</button></div>
            </div>

            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Run 이름</th><th>Version</th><th>범위</th><th>QA 모드</th><th>Run 타입</th><th>실행자</th><th>시작</th><th>런타임</th><th>Source</th><th>QA 상태</th><th>이벤트 수</th><th>제어</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.length === 0 && <tr><td colSpan={12} className="muted">세션이 없습니다.</td></tr>}
                  {sessions.map((s) => (
                    <tr key={s.session_id} className={selectedSessionId === s.session_id ? 'selected-row' : ''}>
                      <td>
                        <div className="run-name-cell">
                          <button className="table-link-btn run-title-btn" onClick={() => loadSessionDetail(s.session_id)}>
                            {buildRunTitle(s)}
                          </button>
                          <div className="run-subline muted">{buildRunSubline(s)}</div>
                        </div>
                      </td>
                      <td>{s.version_name || versionNameById[s.version_id] || '-'}</td>
                      <td>{s.test_scope || '-'}</td>
                      <td>
                        <div>{s.qa_mode}</div>
                        <div className="run-subline muted">{buildQaModeStatusLine(s)}</div>
                      </td>
                      <td>{s.run_type_label}</td>
                      <td>{s.tester}</td>
                      <td>{formatKstDateTime(s.started_at)}</td>
                      <td>{s.runtime_status}</td>
                      <td>{s.analytics_source_label || 'Unknown'}</td>
                      <td><StatusPill value={s.qa_status} /></td>
                      <td>{s.captured_events}</td>
                      <td>
                        {(s.runtime_status === 'running' || s.runtime_status === 'stopping') ? (
                          <button className="danger-btn" onClick={() => onStopSession(s.session_id)}>중지</button>
                        ) : (
                          <button className="danger-btn" onClick={() => onDeleteSession(s.session_id)}>삭제</button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="card detail-card">
              <h3>Session Detail</h3>
              {!sessionDetail?.session_id && <p className="muted">세션을 선택해 주세요.</p>}
              {!!sessionDetail?.session_id && (
                <>
                  <div className="detail-grid">
                    <div>
                      <span className="muted">Session</span>
                      <strong>{sessionDetail.summary?.session_id}</strong>
                      <div>
                        <button type="button" className="table-copy-btn" onClick={() => onCopySessionId(sessionDetail.summary?.session_id)}>ID 복사</button>
                      </div>
                    </div>
                    <div><span className="muted">QA Status</span><strong>{sessionDetail.summary?.qa_status}</strong></div>
                    <div><span className="muted">Run Type</span><strong>{sessionDetail.summary?.run_type}</strong></div>
                    <div><span className="muted">QA Mode</span><strong>{sessionDetail.summary?.qa_mode}</strong></div>
                    <div><span className="muted">Runtime</span><strong>{sessionDetail.summary?.runtime_status}</strong></div>
                    <div><span className="muted">Captured</span><strong>{sessionDetail.summary?.captured_events}</strong></div>
                    <div><span className="muted">Source</span><strong>{sessionDetail.summary?.analytics_source_label || '-'}</strong></div>
                  </div>
                  {sessionDetail.summary?.auto_collect_status_text && (
                    <p className="muted">{sessionDetail.summary.auto_collect_status_text}</p>
                  )}
                  <p className="muted">{analyticsSourceText(sessionDetail.summary, selectedSession || latestSession)}</p>
                  <ul>
                    {(sessionDetail.suspicion || []).map((x) => <li key={x}>{x}</li>)}
                  </ul>
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>시간</th><th>source</th><th>event_name</th><th>params preview</th></tr></thead>
                      <tbody>
                        {(sessionDetail.events || []).map((ev, idx) => (
                          <tr key={`${ev.captured_at}-${idx}`}>
                            <td>{formatKstDateTime(ev.captured_at)}</td>
                            <td>{ev.source}</td>
                            <td>{ev.event_name || '-'}</td>
                            <td>{ev.params_preview}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          </section>
        )}

        {menu === 'DefinitionSpecs' && (
          <section>
            <h1>정의서 관리</h1>
            <div className="card form-grid">
              <label className="full-row">정의서 파일 업로드 (CSV/XLSX)
                <input type="file" accept=".csv,.xlsx,.xls,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(e) => setDefinitionUploadFile(e.target.files?.[0] || null)} />
              </label>
              <div><button type="button" onClick={onUploadDefinition}>정의서 업로드</button></div>
              <div><button type="button" className="neutral-btn" onClick={() => { window.location.href = '/definitions/template?format=xlsx' }}>양식 다운로드</button></div>
            </div>
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>이름</th><th>파일명</th><th>업로드 시각</th><th>이벤트 수</th><th>액션</th>
                  </tr>
                </thead>
                <tbody>
                  {definitionSpecs.length === 0 && <tr><td colSpan={5} className="muted">정의서가 없습니다.</td></tr>}
                  {definitionSpecs.map((x) => (
                    <tr key={x.id} className={selectedDefinitionSpecId === x.id ? 'selected-row' : ''}>
                      <td><button className="table-link-btn" onClick={() => setSelectedDefinitionSpecId(x.id)}>{x.name}</button></td>
                      <td>{x.filename}</td>
                      <td>{x.uploaded_at || '-'}</td>
                      <td>{x.event_count || 0}</td>
                      <td><button className="danger-btn" onClick={() => onDeleteDefinition(x.id)}>삭제</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card">
              <h3>정의서 미리보기</h3>
              {!selectedDefinitionSpec && <p className="muted">정의서를 선택해 주세요.</p>}
              {selectedDefinitionSpec && (
                <>
                  <p className="muted">{selectedDefinitionSpec.name} / {selectedDefinitionSpec.filename}</p>
                  <div className="chips">
                    {(selectedDefinitionSpec.columns || []).slice(0, 20).map((c) => (
                      <span key={c} className="chip">{c}</span>
                    ))}
                  </div>
                  <h4>이벤트 목록 (상위 30)</h4>
                  <ul>
                    {(selectedDefinitionSpec.event_names || []).slice(0, 30).map((ev) => (
                      <li key={ev}>{ev}</li>
                    ))}
                  </ul>
                  <h4>샘플 행</h4>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          {(selectedDefinitionSpec.columns || []).slice(0, 8).map((c) => <th key={c}>{c}</th>)}
                        </tr>
                      </thead>
                      <tbody>
                        {(selectedDefinitionSpec.sample_rows || []).length === 0 && (
                          <tr><td className="muted" colSpan={8}>샘플 데이터가 없습니다.</td></tr>
                        )}
                        {(selectedDefinitionSpec.sample_rows || []).map((row, idx) => (
                          <tr key={idx}>
                            {(selectedDefinitionSpec.columns || []).slice(0, 8).map((c) => (
                              <td key={c}>{row?.[c] || '-'}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          </section>
        )}

        {menu === 'Results' && (
          <section>
            <h1>결과 요약</h1>
            {latestSession && (
              <div className="card">
                <h3>{runtimeHeadline(latestSession.runtime_status)}</h3>
                <p className="muted">
                  {latestSession.qa_mode || '-'} · {latestSession.version_name || versionNameById[latestSession.version_id] || '-'} · {formatDuration(latestSession.started_at, latestSession.ended_at)}
                </p>
                <p className="muted">{analyticsSourceText(sessionDetail?.summary, latestSession)}</p>
              </div>
            )}
            {Array.isArray(results.collection_notices) && results.collection_notices.length > 0 && (
              <div className="card">
                <h3>최근 세션 안내</h3>
                <ul>
                  {results.collection_notices.map((x, idx) => <li key={`${idx}-${x}`}>{x}</li>)}
                </ul>
              </div>
            )}
            {(() => {
              const sc = sessionDetail?.summary?.validation_counts || results.status_counts || {}
              const matched = Number(sc.Matched || 0)
              const mismatch = Number(sc.Mismatch || 0)
              const missing = Number(sc.Missing || 0)
              const blocked = Number(sc.Blocked || 0)
              const unchecked = Number(sc.Unchecked || 0)
              const retest = Number(sc['Retest Needed'] || 0)
              const total = matched + mismatch + missing + blocked + unchecked + retest
              const issue = mismatch + missing + blocked + retest
              const decided = Math.max(0, total - unchecked)
              const gaHitDetected = matched > 0
              const sendReady = total > 0 && blocked < total
              const eventNames = (sessionDetail?.events || [])
                .map((e) => e?.event_name)
                .filter((x) => x && x !== '-' && !isSystemEventName(x))
                .filter((x, i, arr) => arr.indexOf(x) === i)
                .slice(0, 12)
              const reasonTemplates = [
                '판정 근거 부족',
                'hit 확인 전 종료',
                '정의서 매칭 대기',
                '이벤트 감지 후 판정 안 됨',
                '세션 상세 확인 필요'
              ]
              const unresolvedItems = Array.from({ length: Math.min(unchecked, 12) }).map((_, i) => {
                const name = eventNames[i] || `미판정 항목 ${i + 1}`
                return { name, reason: reasonTemplates[i % reasonTemplates.length] }
              })
              const exportsAll = Array.isArray(results.exports) ? results.exports : []
              const currentSessionId = String(sessionDetail?.summary?.session_id || selectedSessionId || latestSession?.session_id || '').trim()
              const currentSessionExports = exportsAll.filter((x) => String(x?.session_id || '').trim() === currentSessionId)
              const latestRunId = currentSessionExports.length > 0
                ? String(currentSessionExports[0]?.run_id || '').trim() || extractRunIdFromExportPath(currentSessionExports[0]?.path)
                : ''
              const latestRunFiles = currentSessionExports.filter((x) => {
                const rid = String(x?.run_id || '').trim() || extractRunIdFromExportPath(x?.path)
                return rid === latestRunId
              })
              const pastRunFiles = exportsAll.filter((x) => {
                const sid = String(x?.session_id || '').trim()
                if (sid !== currentSessionId) return true
                const rid = String(x?.run_id || '').trim() || extractRunIdFromExportPath(x?.path)
                return rid !== latestRunId
              })
              const latestRunFileMap = {}
              for (const item of latestRunFiles) {
                const fn = extractFileNameFromExportPath(item?.path)
                if (fn && !latestRunFileMap[fn]) latestRunFileMap[fn] = item
              }
              const currentSessionFiles = [
                latestRunFileMap['qa_review.xlsx'] || latestRunFileMap['qa_result.xlsx'] || latestRunFileMap['qa_result.csv']
              ].filter(Boolean)
              const advancedFiles = latestRunFiles.filter((x) => {
                const fn = extractFileNameFromExportPath(x?.path).toLowerCase()
                if (fn === 'qa_issues_only.xlsx' || fn === 'qa_raw_debug.xlsx') return true
                if (String(x?.tier || '').toLowerCase() === 'advanced') return true
                return false
              })
              let oneLine = '아직 판정이 완료되지 않았습니다.'
              if (total === 0) oneLine = '아직 검증 데이터가 없습니다.'
              else if (unchecked > 0 && decided > 0) oneLine = `이번 세션은 ${total}개 항목 중 ${decided}개만 판정 완료되었고, ${unchecked}개는 아직 미판정입니다.`
              else if (unchecked === total) oneLine = `이번 세션은 ${total}개 항목이 모두 미판정 상태입니다.`
              else if (issue > 0) oneLine = `이번 세션에서 문제 항목 ${issue}개가 확인되었습니다.`
              else oneLine = '이번 세션은 판정이 완료되었고 주요 항목은 정상입니다.'
              const actionItems = []
              if (unchecked > 0) {
                actionItems.push(`미판정 ${unchecked}개가 있어 세션 상세에서 이벤트 타임라인을 먼저 확인하세요.`)
              }
              if (String(latestSession?.qa_mode || '') === '정의서 검증') {
                actionItems.push('정의서 검증 드로워에서 미판정 항목의 매칭 상태를 점검하세요.')
              }
              actionItems.push('판정이 끝나지 않았다면 동일 조건으로 다시 실행하세요.')
              return (
                <>
                  <div className="card">
                    <p className="muted">
                      현재 세션 기준 집계
                    </p>
                    <p className="muted">
                      <span className={`status ${validationStateClass(sessionDetail?.summary?.validation_state)}`}>{validationStateLabel(sessionDetail?.summary?.validation_state)}</span>
                    </p>
                    <p className="muted">
                      마지막 판정 시각: {formatKstDateTime(sessionDetail?.summary?.validation_updated_at)}
                    </p>
                    <p className="muted">
                      판정 완료 {decided} / {total} · 미판정 {unchecked}
                    </p>
                    <p className="muted">
                      문제 항목은 판정 완료 항목 중 Missing/Mismatch/Blocked/Retest 기준입니다.
                    </p>
                    {latestSession?.session_id && <p className="muted">session: {latestSession.session_id}</p>}
                    <div className="stats-grid">
                      <article className="stat-card"><span>검증 항목 수</span><strong>{total}</strong></article>
                      <article className="stat-card"><span>판정 완료</span><strong>{decided}</strong></article>
                      <article className="stat-card"><span>문제 항목</span><strong>{issue}</strong></article>
                      <article className="stat-card"><span>정상 항목</span><strong>{matched}</strong></article>
                      <article className="stat-card"><span>미판정</span><strong>{unchecked}</strong></article>
                    </div>
                  </div>
                  <div className="card">
                    <h3>가장 중요한 한 줄 요약</h3>
                    <p>{oneLine}</p>
                  </div>
                  <div className="card">
                    <h3>목적지 상태</h3>
                    <div className="chips">
                      <span className={`status ${gaHitDetected ? 'matched' : 'missing'}`}>GA4 hit 감지: {gaHitDetected ? '정상' : '미확인'}</span>
                      <span className={`status ${sendReady ? 'matched' : 'blocked'}`}>전송 조건 충족: {sendReady ? '정상' : '확인 필요'}</span>
                      <span className="status unchecked">네트워크 기준 판정: 진행됨</span>
                    </div>
                  </div>
                  <div className="split">
                    <article className="card">
                      <h3>문제 요약</h3>
                      <p className="muted">미판정 항목 {unchecked}개</p>
                      <ul>
                        {unresolvedItems.length === 0 && <li>없음</li>}
                        {unresolvedItems.map((item, idx) => <li key={`${item.name}-${idx}`}>{item.name} — {item.reason}</li>)}
                      </ul>
                      <h3>다음 액션 안내</h3>
                      <p className="muted">추천 액션: 판정이 완료되지 않은 항목부터 확인하세요.</p>
                      <ol>
                        {actionItems.map((item, idx) => <li key={idx}>{item}</li>)}
                      </ol>
                    </article>
                    <article className="card">
                      <h3>상태별 집계</h3>
                      <ul>
                        <li>Matched: {matched}</li>
                        <li>Mismatch: {mismatch}</li>
                        <li>Missing: {missing}</li>
                        <li>Blocked: {blocked}</li>
                        <li>Retest Needed: {retest}</li>
                        <li>Unchecked: {unchecked}</li>
                      </ul>
                    </article>
                  </div>
                  <article className="card">
                    <h3>
                      이번 세션 파일 {currentSessionFiles.length}개
                      <button type="button" className="neutral-btn ml-10" onClick={() => setShowAdvancedExports((v) => !v)}>
                        {showAdvancedExports ? '고급 다운로드 접기' : `고급 다운로드 ${advancedFiles.length}개`}
                      </button>
                      <button type="button" className="neutral-btn ml-10" onClick={() => setShowExportFiles((v) => !v)}>
                        {showExportFiles ? '과거 파일 접기' : `과거 파일 ${pastRunFiles.length}개 보기`}
                      </button>
                    </h3>
                    <ul className="export-list">
                      {currentSessionFiles.length === 0 && <li>이번 세션 파일이 없습니다.</li>}
                      {currentSessionFiles.map((x) => {
                        const fn = extractFileNameFromExportPath(x.path)
                        return (
                          <li key={x.path}>
                            <a href={`/download?project=${encodeURIComponent(project)}&path=${encodeURIComponent(x.path)}`}>{exportLabel(fn)}</a>
                          </li>
                        )
                      })}
                    </ul>
                    {showAdvancedExports && (
                      <>
                        <h3>고급 다운로드</h3>
                        <ul className="export-list">
                          {advancedFiles.length === 0 && <li>고급 다운로드 파일이 없습니다.</li>}
                          {advancedFiles.map((x) => {
                            const fn = extractFileNameFromExportPath(x.path)
                            return (
                              <li key={x.path}>
                                <a href={`/download?project=${encodeURIComponent(project)}&path=${encodeURIComponent(x.path)}`}>{exportLabel(fn)}</a>
                              </li>
                            )
                          })}
                        </ul>
                      </>
                    )}
                    {showExportFiles && (
                      <>
                        <h3>과거 파일</h3>
                      <ul className="export-list">
                        {pastRunFiles.map((x) => (
                          <li key={x.path}><a href={`/download?project=${encodeURIComponent(project)}&path=${encodeURIComponent(x.path)}`}>{x.name}</a></li>
                        ))}
                      </ul>
                      </>
                    )}
                  </article>
                </>
              )
            })()}
          </section>
        )}

        {menu === 'Settings' && (
          <section>
            <h1>설정</h1>
            <div className="card form-grid">
              <label>기본 도메인
                <input
                  placeholder="https://www.example.com"
                  value={settings.base_domain || ''}
                  onChange={(e) => setSettings((prev) => ({ ...prev, base_domain: e.target.value }))}
                />
              </label>
              <label>기본 시작 URL
                <input
                  placeholder="https://www.example.com/main"
                  value={settings.default_start_url || ''}
                  onChange={(e) => setSettings((prev) => ({ ...prev, default_start_url: e.target.value }))}
                />
              </label>
              <label>브라우저 기본값
                <select value={settings.browser || 'chromium'} onChange={(e) => setSettings((prev) => ({ ...prev, browser: e.target.value }))}>
                  <option value="chromium">chromium</option>
                  <option value="chrome">chrome</option>
                  <option value="edge">edge</option>
                </select>
              </label>
              <label>viewport 기본값
                <select value={settings.viewport || 'Desktop 1440x900'} onChange={(e) => setSettings((prev) => ({ ...prev, viewport: e.target.value }))}>
                  <option value="Desktop 1440x900">Desktop 1440x900</option>
                  <option value="Desktop 1920x1080">Desktop 1920x1080</option>
                  <option value="Mobile 390x844">Mobile 390x844</option>
                </select>
              </label>
              <label>기본 수집 옵션
                <select value={settings.collection_option || 'Auto Crawl'} onChange={(e) => setSettings((prev) => ({ ...prev, collection_option: e.target.value }))}>
                  <option value="Auto Crawl">Auto Crawl</option>
                  <option value="Manual + Auto">Manual + Auto</option>
                  <option value="Manual Only">Manual Only</option>
                </select>
              </label>
              <label>기본 판정 기준
                <select value={settings.judgement_rule || 'Network First'} onChange={(e) => setSettings((prev) => ({ ...prev, judgement_rule: e.target.value }))}>
                  <option value="Network First">Network First</option>
                  <option value="Tag First">Tag First</option>
                  <option value="Combined Strict">Combined Strict</option>
                </select>
              </label>
              <label className="full-row">저장된 페이지 목록 (이름|URL, 줄바꿈)
                <textarea
                  rows={5}
                  value={settings.saved_start_pages_text || ''}
                  onChange={(e) => setSettings((prev) => ({ ...prev, saved_start_pages_text: e.target.value }))}
                  placeholder={'랭킹 메인|https://www.example.com/ranking\\n브랜드 홈|https://www.example.com/brand'}
                />
              </label>
              <label className="full-row">시나리오 페이지군 (그룹명|URL1,URL2,URL3)
                <textarea
                  rows={5}
                  value={settings.scenario_page_groups_text || ''}
                  onChange={(e) => setSettings((prev) => ({ ...prev, scenario_page_groups_text: e.target.value }))}
                  placeholder={'구매플로우|https://www.example.com/cart,https://www.example.com/checkout'}
                />
              </label>
              <div><button type="button" onClick={onSaveSettings}>설정 저장</button></div>
            </div>
          </section>
        )}
      </main>
    </div>
  )
}

function VersionRow({ version, onAction, rowIndex, isDeleted = false }) {
  const [form, setForm] = useState({
    display_name: version.display_name || '',
    status: (version.status === 'Deleted' ? 'Archived' : (version.status || 'Draft')),
    definition_link: version.definition_link || '',
    change_reason: version.change_reason || ''
  })

  useEffect(() => {
    setForm({
      display_name: version.display_name || '',
      status: version.status || 'Draft',
      definition_link: version.definition_link || '',
      change_reason: version.change_reason || ''
    })
  }, [version.id, version.display_name, version.status, version.definition_link, version.change_reason])

  return (
    <tr className={rowIndex % 2 === 1 ? 'alt-row' : ''}>
      <td><input value={form.display_name} onChange={(e) => setForm((prev) => ({ ...prev, display_name: e.target.value }))} /></td>
      <td>{version.created_at || '-'}</td>
      <td>
        {isDeleted ? (
          <span className="muted">Deleted</span>
        ) : (
          <select value={form.status} onChange={(e) => setForm((prev) => ({ ...prev, status: e.target.value }))}>
            <option value="Active">Active</option>
            <option value="Draft">Draft</option>
            <option value="Archived">Archived</option>
          </select>
        )}
      </td>
      <td><input disabled={isDeleted} value={form.definition_link} onChange={(e) => setForm((prev) => ({ ...prev, definition_link: e.target.value }))} /></td>
      <td><input disabled={isDeleted} value={form.change_reason} onChange={(e) => setForm((prev) => ({ ...prev, change_reason: e.target.value }))} /></td>
      <td>{version.session_count || 0}</td>
      <td>
        <div className="row-actions">
          {isDeleted ? (
            <>
              <button className="neutral-btn" onClick={() => onAction({ ...version, ...form }, 'restore')}>복구</button>
              <button className="danger-btn" onClick={() => onAction({ ...version, ...form }, 'hard_delete')}>완전삭제</button>
            </>
          ) : (
            <>
              <button onClick={() => onAction({ ...version, ...form }, 'save')}>편집</button>
              <button className="neutral-btn" onClick={() => onAction({ ...version, ...form }, 'archive')}>보관</button>
              <button className="neutral-btn" onClick={() => onAction({ ...version, ...form }, 'set_default')}>대표 설정</button>
              <button className="danger-btn" onClick={() => onAction({ ...version, ...form }, 'delete')}>삭제</button>
            </>
          )}
        </div>
      </td>
    </tr>
  )
}
