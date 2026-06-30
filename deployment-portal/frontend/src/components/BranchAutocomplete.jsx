import React, { useState, useEffect, useRef } from 'react'
import { api } from '../api.js'

export default function BranchAutocomplete({ serviceKey, value, onChange, placeholder }) {
  const [suggestions, setSuggestions] = useState([])
  const [open, setOpen] = useState(false)
  const debounceRef = useRef(null)

  useEffect(() => {
    if (!serviceKey) { setSuggestions([]); return }
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(async () => {
      try {
        const branches = await api.getBranches(serviceKey, value || '')
        setSuggestions(branches)
      } catch {
        setSuggestions([])
      }
    }, 200)
    return () => clearTimeout(debounceRef.current)
  }, [serviceKey, value])

  return (
    <div style={{ position: 'relative' }}>
      <input
        type="text"
        value={value || ''}
        disabled={!serviceKey}
        placeholder={serviceKey ? (placeholder || 'Type to search branches...') : 'Select a service first'}
        onChange={(e) => { onChange(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
      />
      {open && suggestions.length > 0 && (
        <div className="autocomplete-list">
          {suggestions.map((b) => (
            <div
              key={b}
              className="autocomplete-item"
              onMouseDown={() => { onChange(b); setOpen(false) }}
            >
              {b}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
