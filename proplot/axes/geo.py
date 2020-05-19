#!/usr/bin/env python3
"""
Axes filled with cartographic projections.
"""
import numpy as np
import matplotlib.axes as maxes
import matplotlib.axis as maxis
import matplotlib.text as mtext
import matplotlib.path as mpath
import matplotlib.ticker as mticker
from . import base
from .plot import (
    _basemap_norecurse, _basemap_redirect, _cmap_changer, _cycle_changer,
    _default_latlon, _default_transform,
    _fill_between_wrapper, _fill_betweenx_wrapper,
    _indicate_error, _plot_wrapper,
    _scatter_wrapper, _standardize_1d, _standardize_2d,
    _text_wrapper,
)
from .. import constructor
from .. import crs as pcrs
from ..config import rc
from ..internals import ic  # noqa: F401
from ..internals import docstring, warnings, _version, _version_cartopy, _not_none
try:
    from cartopy.mpl.geoaxes import GeoAxes as GeoAxesBase
    import cartopy.feature as cfeature
    import cartopy.mpl.ticker as cticker
    import cartopy.crs as ccrs
except ModuleNotFoundError:
    GeoAxesBase = object
    cfeature = cticker = ccrs = None
try:
    import mpl_toolkits.basemap as mbasemap
except ModuleNotFoundError:
    mbasemap = None

__all__ = ['GeoAxes', 'BasemapAxes', 'CartopyAxes']


def _circle_boundary(N=100):
    """
    Return a circle `~matplotlib.path.Path` used as the outline for polar
    stereographic, azimuthal equidistant, Lambert conformal, and gnomonic
    projections. This was developed from `this cartopy example \
<https://scitools.org.uk/cartopy/docs/v0.15/examples/always_circular_stereo.html>`__.
    """
    theta = np.linspace(0, 2 * np.pi, N)
    center, radius = [0.5, 0.5], 0.5
    verts = np.vstack([np.sin(theta), np.cos(theta)]).T
    return mpath.Path(verts * radius + center)


class _GeoAxis(object):
    """
    Dummy axis used with longitude and latitude locators and for storing
    view limits on longitude latitude coordinates.
    """
    # NOTE: Due to cartopy bug (https://github.com/SciTools/cartopy/issues/1564)
    # we store presistent longitude and latitude locators on axes, then *call*
    # them whenever set_extent is called and apply *fixed* locators.
    # NOTE: This is modeled after _DummyAxis in matplotlib/ticker.py, but we just
    # needs a couple methods used by simple, linear locators like `MaxNLocator`,
    # `MultipleLocator`, and `AutoMinorLocator`.
    def __init__(self, axes):
        self.axes = axes
        self.major = maxis.Ticker()
        self.minor = maxis.Ticker()
        self.isDefault_majfmt = True
        self.isDefault_majloc = True
        self.isDefault_minloc = True
        self._interval = None

    def get_scale(self):
        return 'linear'

    def get_tick_space(self):
        # Just use the long-standing default of nbins=9
        return 9

    def get_major_formatter(self):
        return self.major.formatter

    def get_major_locator(self):
        return self.major.locator

    def get_minor_locator(self):
        return self.minor.locator

    def get_majorticklocs(self):
        ticks = self.major.locator()
        return self._constrain_ticks(ticks)

    def get_minorticklocs(self):
        ticks = self.minor.locator()
        return self._constrain_ticks(ticks)

    def set_major_formatter(self, formatter, default=False):
        # NOTE: Cartopy formatters check Formatter.axis.axes.projection and has
        # special projection-dependent behavior.
        self.major.formatter = formatter
        formatter.set_axis(self)
        self.isDefault_majfmt = default

    def set_major_locator(self, locator, default=False):
        self.major.locator = locator
        if self.major.formatter:
            self.major.formatter._set_locator(locator)
        locator.set_axis(self)
        self.isDefault_majloc = default

    def set_minor_locator(self, locator, default=False):
        self.minor.locator = locator
        locator.set_axis(self)
        self.isDefault_majfmt = default

    def set_view_interval(self, vmin, vmax):
        self._interval = (vmin, vmax)


class _LonAxis(_GeoAxis):
    """
    Axis with default longitude locator.
    """
    # NOTE: Basemap accepts tick formatters with drawmeridians(fmt=Formatter())
    # Try to use cartopy formatter if cartopy installed. Otherwise use
    # default builtin basemap formatting.
    def __init__(self, axes, projection=None):
        super().__init__(axes)
        locator = formatter = 'deglon'
        if cticker is not None and isinstance(projection, (ccrs._RectangularProjection, ccrs.Mercator)):  # noqa: E501
            locator = formatter = 'dmslon'
        self.set_major_formatter(constructor.Formatter(formatter), default=True)
        self.set_major_locator(constructor.Locator(locator), default=True)
        self.set_minor_locator(mticker.AutoMinorLocator(), default=True)

    def _constrain_ticks(self, ticks):
        eps = 5e-10  # more than 1e-10 because we use 1e-10 in _LongitudeLocator
        ticks = sorted(ticks)
        while ticks and ticks[-1] - eps > ticks[0] + 360 + eps:  # cut off looped ticks
            ticks = ticks[:-1]
        return ticks

    def get_view_interval(self):
        # NOTE: Proplot tries to set its *own* view intervals to avoid dateline
        # weirdness, but if cartopy.autoextent is False the interval will be
        # unset, so we are forced to use get_extent().
        interval = self._interval
        if interval is None:
            extent = self.axes.get_extent()
            interval = extent[:2]  # longitude extents
        return interval


class _LatAxis(_GeoAxis):
    """
    Axis with default latitude locator.
    """
    def __init__(self, axes, latmax=90, projection=None):
        # NOTE: Need to pass projection because lataxis/lonaxis are
        # initialized before geoaxes is initialized, because format() needs
        # the axes and format() is called by proplot.axes.Axes.__init__()
        self._latmax = latmax
        super().__init__(axes)
        locator = formatter = 'deglat'
        if cticker is not None and isinstance(projection, (ccrs._RectangularProjection, ccrs.Mercator)):  # noqa: E501
            locator = formatter = 'dmslat'
        self.set_major_formatter(constructor.Formatter(formatter), default=True)
        self.set_major_locator(constructor.Locator(locator), default=True)
        self.set_minor_locator(mticker.AutoMinorLocator(), default=True)

    def _constrain_ticks(self, ticks):
        # Limit tick latitudes to satisfy latmax
        latmax = self.get_latmax()
        ticks = [l for l in ticks if -latmax <= l <= latmax]
        # Adjust latitude ticks to fix bug in some projections. Harmless for basemap.
        # NOTE: Maybe fixed by cartopy v0.18?
        eps = 1e-10
        if ticks[0] == -90:
            ticks[0] += eps
        if ticks[-1] == 90:
            ticks[-1] -= eps
        return ticks

    def get_latmax(self):
        return self._latmax

    def get_view_interval(self):
        interval = self._interval
        if interval is None:
            extent = self.axes.get_extent()
            interval = extent[2:]  # latitudes
        return interval

    def set_latmax(self, latmax):
        self._latmax = latmax


class GeoAxes(base.Axes):
    """
    Axes subclass for plotting on cartographic projections.
    Adds the `~GeoAxes.format` method and overrides several existing methods.
    Subclassed by `CartopyAxes` and `BasemapAxes`, which respectively use
    cartopy `~cartopy.crs.Projection` and basemap `~mpl_toolkits.basemap.Basemap`
    objects to calculate projection coordinates.
    """
    def __init__(self, *args, **kwargs):
        """
        See also
        --------
        proplot.ui.subplots
        proplot.axes.CartopyAxes
        proplot.axes.BasemapAxes
        """
        self._boundinglat = None  # latitude bound for polar projections
        super().__init__(*args, **kwargs)

    @docstring.add_snippets
    def format(
        self, *,
        lonlim=None, latlim=None, boundinglat=None,
        longrid=None, latgrid=None, longridminor=None, latgridminor=None,
        lonlocator=None, lonlines=None,
        latlocator=None, latlines=None, latmax=None,
        lonminorlocator=None, lonminorlines=None,
        latminorlocator=None, latminorlines=None,
        lonlocator_kw=None, lonlines_kw=None,
        latlocator_kw=None, latlines_kw=None,
        lonminorlocator_kw=None, lonminorlines_kw=None,
        latminorlocator_kw=None, latminorlines_kw=None,
        lonformatter=None, latformatter=None,
        lonformatter_kw=None, latformatter_kw=None,
        labels=None, latlabels=None, lonlabels=None,
        loninline=None, latinline=None, rotate_labels=None, labelpad=None, dms=None,
        patch_kw=None, **kwargs,
    ):
        """
        Modify the longitude and latitude labels, longitude and latitude map
        limits, geographic features, and more. Unknown keyword arguments are
        passed to `Axes.format` and `~proplot.config.rc_configurator.context`.

        Parameters
        ----------
        lonlim, latlim : (float, float), optional
            *For cartopy axes only.*
            The approximate longitude and latitude boundaries of the map,
            applied with `~cartopy.mpl.geoaxes.GeoAxes.set_extent`.
            Basemap axes extents must be declared when instantiating the
            `~mpl_toolkits.basemap.Basemap` object.
        boundinglat : float, optional
            *For cartopy axes only.*
            The edge latitude for the circle bounding North Pole and
            South Pole-centered projections.
            Basemap bounding latitudes must be declared when instantiating the
            `~mpl_toolkits.basemap.Basemap` object.
        longrid, latgrid : bool, optional
            Whether to draw longitude and latitude gridlines.
            Default is :rc:`grid`. Use `grid` to toggle both.
        longridminor, latgridminor : bool, optional
            Whether to draw "minor" longitude and latitude lines.
            Default is :rc:`gridminor`. Use `gridminor` to toggle both.
        lonlocator, latlocator : str, float, list of float, or \
`~matplotlib.ticker.Locator`, optional
            Used to determine the longitude and latitude gridline locations.
            Passed to the `~proplot.constructor.Locator` constructor. Can be
            string, float, list of float, or `matplotlib.ticker.Locator` instance.
            The defaults are custom proplot

            Locator spec used to determine longitude and latitude gridline locations.
            If float, indicates the *step size* between longitude and latitude
            gridlines. If list of float, indicates the exact longitude and latitude
            gridline locations. Otherwise, the argument is interpreted by the
            `~proplot.constructor.Locator` constructor, and the defaults are ProPlot's
            custom longitude and latitude locators adapted from cartopy.
        lonlines, latlines : optional
            Aliases for `lonlocator`, `latlocator`.
        lonlocator_kw, latlocator_kw : dict, optional
            Keyword argument dictionaries passed to the `~matplotlib.ticker.Locator`
            class.
        lonlines_kw, latlines_kw : optional
            Aliases for `lonlocator_kw`, `latlocator_kw`.
        lonminorlocator, latminorlocator, lonminorlines, latminorlines : optional
            As with `lonlocator` and `latlocator` but for the "minor" gridlines.
            The defaults are :rc:`grid.lonminorstep` and :rc:`grid.latminorstep`.
        lonminorlocator_kw, latminorlocator_kw, lonminorlines_kw, latminorlines_kw : \
optional
            As with `lonminorlocator_kw` and `latminorlocator_kw` but for the "minor"
            gridlines.
        latmax : float, optional
            The maximum absolute latitude for longitude and latitude gridlines.
            Longitude gridlines are cut off poleward of this latitude for *all*
            basemap projections and the *subset* of cartopy projections that support
            this feature. Default is ``80``.
        latmax : float, optional
            Maximum absolute gridline latitude. Lines poleward of this latitude are cut
            off for *all* basemap projections and the *subset* of cartopy projections
            that support this feature.
        labels : bool, optional
            Toggles longitude and latitude gridline labels on and off. Default
            is :rc:`grid.labels`.
        lonlabels, latlabels
            Whether to label longitudes and latitudes, and on which sides
            of the map. There are four different options:

            1. Boolean ``True``. Indicates left side for latitudes,
               bottom for longitudes.
            2. A string indicating the side names, e.g. ``'lr'`` or ``'bt'``.
            3. A boolean 2-tuple indicating whether to draw labels on the
               ``(left, right)`` sides for longitudes, or ``(bottom, top)``
               sides for latitudes.
            4. A boolean 4-tuple indicating whether to draw labels on the
               ``(left, right, bottom, top)`` sides, as in the basemap
               `~mpl_toolkits.basemap.Basemap.drawmeridians` and
               `~mpl_toolkits.basemap.Basemap.drawparallels` methods.

        lonformatter, latformatter : str or `~matplotlib.ticker.Formatter`, optional
            Formatter spec used to style longitude and latitude gridline labels.
            Passed to the `~proplot.constructor.Formatter` constructor.
            Can be string, list of string, or `matplotlib.ticker.Formatter`
            instance. Use ``[]`` or ``'null'`` for no ticks. The defaults are the
            ``'deglon'`` and ``'deglat'`` `~proplot.ticker.AutoFormatter` presets.
            For cartopy rectangular projections, the defaults are cartopy's fancy
            are `~cartopy.mpl.ticker.LongitudeFormatter` and
            `~cartopy.mpl.ticker.LatitudeFormatter` formatters with
            degrees-minutes-seconds support.
        lonformatter_kw, latformatter_kw : optional
            Keyword argument dictionaries passed to the `~matplotlib.ticker.Formatter`
            class.
        loninline, latinline : bool, optional
            *For cartopy axes only.*
            Whether to draw inline longitude and latitude gridline labels.
            Defaults are :rc:`grid.loninline` and :rc:`grid.latinline`.
        rotate_labels : bool, optional
            *For cartopy axes only.*
            Whether to rotate longitude and latitude gridline labels.
            Default is :rc:`grid.rotatelabels`.
        labelpad : float, optional
            *For cartopy axes only.*
            Controls the padding between the map boundary and longitude and
            latitude gridline labels. Default is :rc:`grid.labelpad`.
        dms : bool, optional
            *For cartopy axes only.*
            Specifies whether the cartopy `~cartopy.mpl.ticker.LongitudeFormatter`
            and `~cartopy.mpl.ticker.LatitudeFormatter` should use
            degrees-minutes-seconds for gridline labels on small scales in
            rectangular projections. Default is ``True``.
        land, ocean, coast, rivers, lakes, borders, innerborders : bool, \
optional
            Toggles various geographic features. These are actually the
            :rcraw:`land`, :rcraw:`ocean`, :rcraw:`coast`, :rcraw:`rivers`,
            :rcraw:`lakes`, :rcraw:`borders`, and :rcraw:`innerborders`
            settings passed to `~proplot.config.rc_configurator.context`.
            The style can be modified using additional `rc` settings. For example,
            to change :rcraw:`land.color`, use ``ax.format(landcolor='green')``,
            and to change :rcraw:`land.zorder`, use ``ax.format(landzorder=4)``.
        reso : {'lo', 'med', 'hi', 'x-hi', 'xx-hi'}
            *For cartopy axes only.*
            Resolution of geographic features. For basemap axes, this must be
            passed when the projection is created with `~proplot.constructor.Proj`.
        %(axes.patch_kw)s

        Other parameters
        ----------------
        %(axes.other)s

        See also
        --------
        proplot.axes.Axes.format
        proplot.config.rc_configurator.context
        """
        # Format axes
        rc_kw, rc_mode, kwargs = self._parse_format(**kwargs)
        with rc.context(rc_kw, mode=rc_mode):
            # Gridline toggles
            grid = rc.get('grid', context=True)
            gridminor = rc.get('gridminor', context=True)
            longrid = _not_none(longrid, grid)
            latgrid = _not_none(latgrid, grid)
            longridminor = _not_none(longridminor, gridminor)
            latgridminor = _not_none(latgridminor, gridminor)

            # Label toggles
            labels = _not_none(labels, rc.get('grid.labels', context=True))
            lonlabels = _not_none(lonlabels, labels)
            latlabels = _not_none(latlabels, labels)
            lonarray = self._to_label_array(lonlabels, lon=True)
            latarray = self._to_label_array(latlabels, lon=False)

            # Update 'maximum latitude'
            latmax = _not_none(latmax, rc.get('grid.latmax', context=True))
            if latmax is not None:
                self._lataxis.set_latmax(latmax)

            # Update major locators
            lonlocator = _not_none(lonlocator=lonlocator, lonlines=lonlines)
            latlocator = _not_none(latlocator=latlocator, latlines=latlines)
            if lonlocator is not None:
                lonlocator_kw = _not_none(
                    lonlocator_kw=lonlocator_kw, lonlines_kw=lonlines_kw, default={},
                )
                locator = constructor.Locator(lonlocator, **lonlocator_kw)
                self._lonaxis.set_major_locator(locator)
            if latlocator is not None:
                latlocator_kw = _not_none(
                    latlocator_kw=latlocator_kw, latlines_kw=latlines_kw, default={},
                )
                locator = constructor.Locator(latlocator, **latlocator_kw)
                self._lataxis.set_major_locator(locator)

            # Update minor locators
            lonminorlocator = _not_none(
                lonminorlocator=lonminorlocator, lonminorlines=lonminorlines
            )
            latminorlocator = _not_none(
                latminorlocator=latminorlocator, latminorlines=latminorlines
            )
            if lonminorlocator is not None:
                lonminorlocator_kw = _not_none(
                    lonminorlocator_kw=lonminorlocator_kw,
                    lonminorlines_kw=lonminorlines_kw,
                    default={},
                )
                locator = constructor.Locator(lonminorlocator, **lonminorlocator_kw)
                self._lonaxis.set_minor_locator(locator)
            if latminorlocator is not None:
                latminorlocator_kw = _not_none(
                    latminorlocator_kw=latminorlocator_kw,
                    latminorlines_kw=latminorlines_kw,
                    default={},
                )
                locator = constructor.Locator(latminorlocator, **latminorlocator_kw)
                self._lataxis.set_minor_locator(locator)

            # Update formatters
            loninline = _not_none(loninline, rc.get('grid.loninline', context=True))
            latinline = _not_none(latinline, rc.get('grid.latinline', context=True))
            labelpad = _not_none(labelpad, rc.get('grid.labelpad', context=True))
            rotate_labels = _not_none(
                rotate_labels, rc.get('grid.rotatelabels', context=True)
            )
            if lonformatter is not None:
                lonformatter_kw = lonformatter_kw or {}
                formatter = constructor.Formatter(lonformatter, **lonformatter_kw)
                self._lonaxis.set_major_formatter(formatter)
            if latformatter is not None:
                latformatter_kw = latformatter_kw or {}
                formatter = constructor.Formatter(latformatter, **latformatter_kw)
                self._lataxis.set_major_formatter(formatter)
            if dms is not None:
                self._lonaxis.get_major_formatter()._dms = dms
                self._lataxis.get_major_formatter()._dms = dms
                self._lonaxis.get_major_locator()._dms = dms
                self._lataxis.get_major_locator()._dms = dms

            # Apply worker functions
            self._update_extent(lonlim=lonlim, latlim=latlim, boundinglat=boundinglat)
            self._update_patches(patch_kw or {})
            self._update_features()
            self._update_major_gridlines(
                longrid=longrid, latgrid=latgrid,  # gridline toggles
                lonarray=lonarray, latarray=latarray,  # label toggles
                loninline=loninline, latinline=latinline, rotate_labels=rotate_labels,
                labelpad=labelpad,
            )
            self._update_minor_gridlines(longrid=longridminor, latgrid=latgridminor)

            # Call main axes format method
            super().format(**kwargs)

    @staticmethod
    def _axis_below_to_zorder(axisbelow):
        """
        Get the zorder for an axisbelow setting.
        """
        if axisbelow is True:
            zorder = 0.5
        elif axisbelow is False:
            zorder = 2.5
        elif axisbelow == 'lines':
            zorder = 1.5
        else:
            raise ValueError(f'Unexpected grid.below value {axisbelow!r}.')
        return zorder

    def _get_gridline_props(self, which='major', context=True):
        """
        Get dictionaries of gridline properties for lines and text.
        """
        # Line properties
        # WARNING: Here we use apply existing *matplotlib* rc param to brand new
        # *proplot* setting. So if rc mode is 1 (first format call) use context=False.
        key = 'grid' if which == 'major' else 'gridminor'
        kwlines = rc.fill(
            {
                'alpha': f'{key}.alpha',
                'color': f'{key}.color',
                'linewidth': f'{key}.linewidth',
                'linestyle': f'{key}.linestyle',
            },
            context=context,
        )
        axisbelow = rc.get('axes.axisbelow', context=True)
        if axisbelow is not None:
            kwlines['zorder'] = self._axis_below_to_zorder(axisbelow)

        # Text properties
        kwtext = {}
        if which == 'major':
            kwtext = rc.fill(
                {
                    'color': f'{key}.color',
                    'fontsize': f'{key}.labelsize',
                    'weight': f'{key}.labelweight',
                },
                context=context,
            )

        return kwlines, kwtext

    def _get_lonticklocs(self, which='major'):
        """
        Retrieve longitude tick locations.
        """
        # Get tick locations from dummy axes
        # NOTE: This is workaround for: https://github.com/SciTools/cartopy/issues/1564
        # Since _axes_domain is wrong we determine tick locations ourselves with
        # more accurate extent tracked by _LatAxis and _LonAxis.
        axis = self._lonaxis
        if which == 'major':
            lines = axis.get_majorticklocs()
        else:
            lines = axis.get_minorticklocs()
        return lines

    def _get_latticklocs(self, which='major'):
        """
        Retrieve latitude tick locations.
        """
        axis = self._lataxis
        if which == 'major':
            lines = axis.get_majorticklocs()
        else:
            lines = axis.get_minorticklocs()
        return lines

    def _set_view_intervals(self, extent):
        """
        Update view intervals for lon and lat axis.
        """
        self._lonaxis.set_view_interval(*extent[:2])
        self._lataxis.set_view_interval(*extent[2:])

    @staticmethod
    def _to_label_array(labels, lon=True):
        """
        Convert labels argument to length-4 boolean array.
        """
        if labels is None:
            return (None,) * 4
        if isinstance(labels, str):
            array = [0] * 4
            for idx, char in zip([0, 1, 2, 3], 'lrbt'):
                if char in labels:
                    array[idx] = 1
        else:
            array = np.atleast_1d(labels)
        if len(array) == 1:
            array = [*array, 0]  # default is to label bottom or left
        if len(array) == 2:
            if lon:
                array = [0, 0, *array]
            else:
                array = [*array, 0, 0]
        elif len(array) != 4:
            name = 'lon' if lon else 'lat'
            raise ValueError(f'Invalid {name}label spec: {labels}.')
        return array


class CartopyAxes(GeoAxes, GeoAxesBase):
    """
    Axes subclass for plotting
    `cartopy <https://scitools.org.uk/cartopy/docs/latest/>`__ projections.
    Makes ``transform=cartopy.crs.PlateCarree()`` the default for all plotting methods,
    enforces `global extent <https://stackoverflow.com/a/48956844/4970632>`__
    for most projections by default,  and draws `circular boundaries \
<https://scitools.org.uk/cartopy/docs/latest/gallery/always_circular_stereo.html>`__
    around polar azimuthal, stereographic, and Gnomonic projections bounded at
    the equator by default.
    """
    #: The registered projection name.
    name = 'cartopy'
    _proj_north = (
        pcrs.NorthPolarStereo,
        pcrs.NorthPolarGnomonic,
        pcrs.NorthPolarAzimuthalEquidistant,
        pcrs.NorthPolarLambertAzimuthalEqualArea,
    )
    _proj_south = (
        pcrs.SouthPolarStereo,
        pcrs.SouthPolarGnomonic,
        pcrs.SouthPolarAzimuthalEquidistant,
        pcrs.SouthPolarLambertAzimuthalEqualArea
    )
    _proj_polar = _proj_north + _proj_south

    def __init__(self, *args, map_projection=None, **kwargs):
        """
        Parameters
        ----------
        map_projection : `~cartopy.crs.Projection`
            The `~cartopy.crs.Projection` instance.

        Other parameters
        ----------------
        *args, **kwargs
            Passed to `~cartopy.mpl.geoaxes.GeoAxes`.

        See also
        --------
        proplot.axes.GeoAxes
        proplot.constructor.Proj
        """
        # GeoAxes initialization. Note that critical attributes like
        # outline_patch needed by _format_apply are added before it is called.
        # NOTE: Initial extent is configured in _update_extent
        import cartopy  # noqa: F401
        if not isinstance(map_projection, ccrs.Projection):
            raise ValueError('GeoAxes requires map_projection=cartopy.crs.Projection.')
        latmax = 90
        boundinglat = None
        polar = isinstance(map_projection, self._proj_polar)
        if polar:
            latmax = 80
            boundinglat = 0
            if isinstance(map_projection, pcrs.NorthPolarGnomonic):
                boundinglat = 30  # *default* bounding latitudes
            elif isinstance(map_projection, pcrs.SouthPolarGnomonic):
                boundinglat = -30

        # Initialize axes
        self._boundinglat = boundinglat
        self._map_projection = map_projection  # cartopy also does this
        self._gridlines_major = None
        self._gridlines_minor = None
        self._lonaxis = _LonAxis(self, projection=map_projection)
        self._lataxis = _LatAxis(self, latmax=latmax, projection=map_projection)
        super().__init__(*args, map_projection=map_projection, **kwargs)

        # Apply circular map boundary for polar projections. Apply default
        # global extent for other projections.
        # NOTE: This has to come after initialization, and we override set_global
        # so it also updates _LatAxis and _LonAxis. Also want to use set_global
        # rather than _update_extent([-180 + lon0, 180 + lon0, -90, 90]) in case
        # projection extent cannot be global.
        if polar and hasattr(self, 'set_boundary'):
            self.set_boundary(_circle_boundary(), transform=self.transAxes)
        if not rc['cartopy.autoextent']:
            if polar:
                self._update_extent(boundinglat=self._boundinglat)
            else:
                self.set_global()

        # Zero out ticks to prevent extra label offset
        for axis in (self.xaxis, self.yaxis):
            axis.set_tick_params(which='both', size=0)

    def _apply_axis_sharing(self):  # noqa: U100
        """
        No-op for now. In future will hide labels on certain subplots.
        """
        pass

    def _get_current_extent(self):
        """
        Get extent as last set from `~cartopy.mpl.geoaxes.GeoAxes.set_extent`.
        """
        # NOTE: This is *also* not perfect because if set_extent() was called
        # and extent crosses map boundary of rectangular projection, the *actual*
        # resulting extent is the opposite. But that means user has messed up anyway
        # so probably doesn't matter if gridlines are also wrong.
        if not self._current_extent:
            lon0 = self._get_lon0()
            self._current_extent = [-180 + lon0, 180 + lon0, -90, 90]
        return self._current_extent

    def _get_lon0(self):
        """
        Get the central longitude. Default is ``0``.
        """
        return self.projection.proj4_params.get('lon_0', 0)

    def _init_gridlines(self):
        """
        Create monkey patched "major" and "minor" gridliners managed by ProPlot.
        """
        # Cartopy 0.18 monkey patch. This fixes issue where we get overlapping
        # gridlines on dateline. See the "nx -= 1" line in Gridliner._draw_gridliner
        # TODO: Submit cartopy PR. This is awful but necessary for quite a while if
        # the time between v0.17 and v0.18 is any indication.
        def _draw_gridliner(self, *args, **kwargs):
            result = type(self)._draw_gridliner(self, *args, **kwargs)
            if _version_cartopy == _version('0.18'):
                lon_lim, _ = self._axes_domain()
                if abs(np.diff(lon_lim)) == abs(np.diff(self.crs.x_limits)):
                    for collection in self.xline_artists:
                        if not getattr(collection, '_cartopy_fix', False):
                            collection.get_paths().pop(-1)
                            collection._cartopy_fix = True
            return result

        # Cartopy < 0.18 monkey patch. This is part of filtering valid label coordinates
        # to values between lon_0 - 180 and lon_0 + 180.
        def _axes_domain(self, *args, **kwargs):
            x_range, y_range = type(self)._axes_domain(self, *args, **kwargs)
            if _version_cartopy < _version('0.18'):
                lon_0 = self.axes.projection.proj4_params.get('lon_0', 0)
                x_range = np.asarray(x_range) + lon_0
            return x_range, y_range

        # Cartopy < 0.18 gridliner method monkey patch. Always print number in range
        # (180W, 180E). We choose #4 of the following choices 3 choices (see Issue #78):
        # 1. lonlines go from -180 to 180, but get double 180 labels at dateline
        # 2. lonlines go from -180 to e.g. 150, but no lines from 150 to dateline
        # 3. lonlines go from lon_0 - 180 to lon_0 + 180 mod 360, but results
        #    in non-monotonic array causing double gridlines east of dateline
        # 4. lonlines go from lon_0 - 180 to lon_0 + 180 monotonic, but prevents
        #    labels from being drawn outside of range (-180, 180)
        def _add_gridline_label(self, value, axis, upper_end):
            if _version_cartopy < _version('0.18'):
                if axis == 'x':
                    value = (value + 180) % 360 - 180
            return type(self)._add_gridline_label(self, value, axis, upper_end)

        # NOTE: The 'xpadding' and 'ypadding' props were introduced in v0.16
        # with default 5 points, then set to default None in v0.18.
        # TODO: Cartopy has had two formatters for a while but we use newer one
        # https://github.com/SciTools/cartopy/pull/1066
        gl = self.gridlines(crs=ccrs.PlateCarree())
        gl._draw_gridliner = _draw_gridliner.__get__(gl)  # apply monkey patch
        gl._axes_domain = _axes_domain.__get__(gl)
        gl._add_gridline_label = _add_gridline_label.__get__(gl)
        gl.xlines = gl.ylines = False
        self._toggle_gridliner_labels(gl, False, False, False, False)
        return gl

    @staticmethod
    def _toggle_gridliner_labels(gl, left, right, bottom, top):
        """
        Toggle gridliner labels across different cartopy versions.
        """
        if _version_cartopy >= _version('0.18'):  # cartopy >= 0.18
            left_labels = 'left_labels'
            right_labels = 'right_labels'
            bottom_labels = 'bottom_labels'
            top_labels = 'top_labels'
        else:  # cartopy < 0.18
            left_labels = 'ylabels_left'
            right_labels = 'ylabels_right'
            bottom_labels = 'xlabels_bottom'
            top_labels = 'xlabels_top'
        if left is not None:
            setattr(gl, left_labels, left)
        if right is not None:
            setattr(gl, right_labels, right)
        if bottom is not None:
            setattr(gl, bottom_labels, bottom)
        if top is not None:
            setattr(gl, top_labels, top)

    def _update_extent(self, lonlim=None, latlim=None, boundinglat=None):
        """
        Set the projection extent.
        """
        # Projection extent
        # NOTE: Lon axis and lat axis extents are updated by set_extent.
        # WARNING: The set_extent method tries to set a *rectangle* between the *4*
        # (x, y) coordinate pairs (each corner), so something like (-180, 180, -90, 90)
        # will result in *line*, causing error! We correct this here.
        eps = 1e-10  # bug with full -180, 180 range when lon_0 != 0
        lon0 = self._get_lon0()
        proj = type(self.projection).__name__
        north = isinstance(self.projection, self._proj_north)
        south = isinstance(self.projection, self._proj_south)
        extent = None
        if north or south:
            if lonlim is not None or latlim is not None:
                warnings._warn_proplot(
                    f'{proj!r} extent is controlled by "boundinglat", '
                    f'ignoring lonlim={lonlim!r} and latlim={latlim!r}.'
                )
            if boundinglat is not None and boundinglat != self._boundinglat:
                lat0 = 90 if north else -90
                lon0 = self._get_lon0()
                extent = [lon0 - 180 + eps, lon0 + 180 - eps, boundinglat, lat0]
                self.set_extent(extent, crs=ccrs.PlateCarree())
                self._boundinglat = boundinglat

        # Rectangular extent
        else:
            if boundinglat is not None:
                warnings._warn_proplot(
                    f'{proj!r} extent is controlled by "lonlim" and "latlim", '
                    f'ignoring boundinglat={boundinglat!r}.'
                )
            if lonlim is not None or latlim is not None:
                lonlim = list(lonlim or [None, None])
                latlim = list(latlim or [None, None])
                if lonlim[0] is None:
                    lonlim[0] = lon0 - 180
                if lonlim[1] is None:
                    lonlim[1] = lon0 + 180
                lonlim[0] += eps
                if latlim[0] is None:
                    latlim[0] = -90
                if latlim[1] is None:
                    latlim[1] = 90
                extent = lonlim + latlim
                self.set_extent(extent, crs=ccrs.PlateCarree())

    def _update_patches(self, patch_kw=None):
        """
        Update the map background based on rc settings.
        """
        # Update background patch
        kw_face = rc.fill(
            {
                'facecolor': 'axes.facecolor',
                'alpha': 'axes.alpha',
            },
            context=True
        )
        kw_edge = rc.fill(
            {
                'edgecolor': 'geoaxes.edgecolor',
                'linewidth': 'geoaxes.linewidth',
            },
            context=True
        )
        kw_face.update(patch_kw or {})
        self.background_patch.update(kw_face)
        self.outline_patch.update(kw_edge)

    def _update_features(self):
        """
        Update geographic features.
        """
        # NOTE: The e.g. cfeature.COASTLINE features are just for convenience,
        # lo res versions. Use NaturalEarthFeature instead.
        # WARNING: Seems cartopy features cannot be updated! Updating _kwargs
        # attribute does *nothing*.
        reso = rc['reso']  # resolution cannot be changed after feature created
        try:
            reso = constructor.CARTOPY_RESOS[reso]
        except KeyError:
            raise ValueError(
                f'Invalid resolution {reso!r}. Options are: '
                + ', '.join(map(repr, constructor.CARTOPY_RESOS)) + '.'
            )
        for name, args in constructor.CARTOPY_FEATURES.items():
            b = rc.get(name, context=True)
            attr = f'_{name}_feature'
            if b is not None:
                feat = getattr(self, attr, None)
                if not b:
                    if feat is not None:  # toggle existing feature off
                        feat.set_visible(False)
                else:
                    drawn = feat is not None  # if exists, apply *updated* settings
                    if not drawn:
                        feat = cfeature.NaturalEarthFeature(*args, reso)
                        feat = self.add_feature(feat)  # convert to FeatureArtist
                    # For 'lines', need to specify edgecolor and facecolor
                    # See: https://github.com/SciTools/cartopy/issues/803
                    kw = rc.category(name, context=drawn)
                    if name in ('coast', 'rivers', 'borders', 'innerborders'):
                        kw.update({'edgecolor': kw.pop('color'), 'facecolor': 'none'})
                    else:
                        kw.update({'linewidth': 0})
                    # Update artist attributes (_kwargs used back to v0.5)
                    # feat.update(kw)  # TODO: check this fails
                    feat._kwargs.update(kw)
                    setattr(self, attr, feat)

    def _update_gridlines(self, gl, which='major', longrid=None, latgrid=None):
        """
        Update gridliner object with axis locators, and toggle gridlines on and off.
        """
        # Update gridliner collection properties
        # WARNING: Here we use apply existing *matplotlib* rc param to brand new
        # *proplot* setting. So if rc mode is 1 (first format call) use context=False.
        rc_mode = rc._get_context_mode()
        kwlines, kwtext = self._get_gridline_props(which=which, context=(rc_mode == 2))
        gl.collection_kwargs.update(kwlines)
        gl.xlabel_style.update(kwtext)
        gl.ylabel_style.update(kwtext)

        # Apply tick locations from dummy _LonAxis and _LatAxis axes
        if longrid is not None:
            gl.xlines = longrid
        if latgrid is not None:
            gl.ylines = latgrid
        gl.xlocator = mticker.FixedLocator(self._get_lonticklocs(which=which))
        gl.ylocator = mticker.FixedLocator(self._get_latticklocs(which=which))

    def _update_major_gridlines(
        self,
        longrid=None, latgrid=None,
        lonarray=None, latarray=None,
        loninline=None, latinline=None, labelpad=None, rotate_labels=None,
    ):
        """
        Update major gridlines.
        """
        # Update gridline locations and style
        if not self._gridlines_major:
            self._gridlines_major = self._init_gridlines()
        gl = self._gridlines_major
        self._update_gridlines(gl, which='major', longrid=longrid, latgrid=latgrid)

        # Updage gridline label parameters
        if labelpad is not None:
            gl.xpadding = gl.ypadding = labelpad
        if loninline is not None:
            gl.x_inline = loninline
        if latinline is not None:
            gl.y_inline = latinline
        if rotate_labels is not None:
            gl.rotate_labels = rotate_labels  # ignored in cartopy <0.18

        # Gridline label formatters
        # TODO: Use lonaxis and lataxis instead
        lonaxis = self._lonaxis
        lataxis = self._lataxis
        gl.xformatter = lonaxis.get_major_formatter()
        gl.yformatter = lataxis.get_major_formatter()

        # Gridline label toggling
        # Issue warning instead of error!
        if _version_cartopy < _version('0.18'):
            if not isinstance(self.projection, (ccrs.Mercator, ccrs.PlateCarree)):
                if any(latarray):
                    warnings._warn_proplot(
                        'Cannot add gridline labels to cartopy '
                        f'{type(self.projection).__name__} projection.'
                    )
                    latarray = [0] * 4
                if any(lonarray):
                    warnings._warn_proplot(
                        'Cannot add gridline labels to cartopy '
                        f'{type(self.projection).__name__} projection.'
                    )
                    lonarray = [0] * 4
        self._toggle_gridliner_labels(gl, *latarray[:2], *lonarray[2:])

    def _update_minor_gridlines(self, longrid=None, latgrid=None):
        """
        Update minor gridlines.
        """
        if not self._gridlines_minor:
            self._gridlines_minor = self._init_gridlines()
        gl = self._gridlines_minor
        self._update_gridlines(gl, which='minor', longrid=longrid, latgrid=latgrid)

    def get_tightbbox(self, renderer, *args, **kwargs):
        # Perform extra post-processing steps
        # For now this just draws the gridliners
        self._apply_axis_sharing()
        if self.get_autoscale_on() and self.ignore_existing_data_limits:
            self.autoscale_view()
        if (
            getattr(self.background_patch, 'reclip', None)
            and hasattr(self.background_patch, 'orig_path')
        ):
            clipped_path = self.background_patch.orig_path.clip_to_bbox(self.viewLim)
            self.background_patch._path = clipped_path

        # Adjust location
        if _version_cartopy >= _version('0.18'):
            self.patch._adjust_location()

        # Apply aspect
        self.apply_aspect()
        for gl in self._gridliners:
            if _version_cartopy >= _version('0.18'):
                gl._draw_gridliner(renderer=renderer)
            else:
                gl._draw_gridliner(background_patch=self.background_patch)

        # Remove gridliners
        if _version_cartopy < _version('0.18'):
            self._gridliners = []

        return super().get_tightbbox(renderer, *args, **kwargs)

    def get_extent(self, crs=None):
        # Get extent and try to repair longitude bounds.
        if crs is None:
            crs = ccrs.PlateCarree()
        extent = super().get_extent(crs=crs)
        if isinstance(crs, ccrs.PlateCarree):
            if np.isclose(extent[0], -180) and np.isclose(extent[-1], 180):
                # Repair longitude bounds to reflect dateline position
                # NOTE: This is critical so we can prevent duplicate gridlines
                # on dateline. See _update_gridlines.
                lon0 = self._get_lon0()
                extent[:2] = [lon0 - 180, lon0 + 180]
        return extent

    def set_extent(self, extent, crs=None):
        # Fix extent, so axes tight bounding box gets correct box! From this issue:
        # https://github.com/SciTools/cartopy/issues/1207#issuecomment-439975083
        # Also record the requested longitude latitude extent so we can use these
        # values for LongitudeLocator and LatitudeLocator. Otherwise if longitude
        # extent is across international dateline LongitudeLocator fails because
        # get_extent() reports -180 to 180.
        # See: https://github.com/SciTools/cartopy/issues/1564
        if crs is None:
            crs = ccrs.PlateCarree()
        if isinstance(crs, ccrs.PlateCarree):
            self._set_view_intervals(extent)
            self._update_gridlines(self._gridlines_major)
            self._update_gridlines(self._gridlines_minor)
            if _version_cartopy < _version('0.18'):
                clipped_path = self.outline_patch.orig_path.clip_to_bbox(self.viewLim)
                self.outline_patch._path = clipped_path
                self.background_patch._path = clipped_path
        return super().set_extent(extent, crs=crs)

    def set_global(self):
        # Set up "global" extent and update _LatAxis and _LonAxis view intervals
        result = super().set_global()
        self._set_view_intervals(self.get_extent())
        return result

    @property
    def projection(self):
        """
        The `~cartopy.crs.Projection` instance associated with this axes.
        """
        return self._map_projection

    @projection.setter
    def projection(self, map_projection):
        if not isinstance(map_projection, ccrs.CRS):
            raise ValueError('Projection must be a cartopy.crs.CRS instance.')
        self._map_projection = map_projection

    # Wrapped methods
    # TODO: Remove this duplication!
    if GeoAxesBase is not object:
        text = _text_wrapper(
            GeoAxesBase.text
        )
        plot = _default_transform(_plot_wrapper(_standardize_1d(
            _indicate_error(_cycle_changer(GeoAxesBase.plot))
        )))
        scatter = _default_transform(_scatter_wrapper(_standardize_1d(
            _indicate_error(_cycle_changer(GeoAxesBase.scatter))
        )))
        fill_between = _fill_between_wrapper(_standardize_1d(_cycle_changer(
            GeoAxesBase.fill_between
        )))
        fill_betweenx = _fill_betweenx_wrapper(_standardize_1d(_cycle_changer(
            GeoAxesBase.fill_betweenx
        )))
        contour = _default_transform(_standardize_2d(_cmap_changer(
            GeoAxesBase.contour
        )))
        contourf = _default_transform(_standardize_2d(_cmap_changer(
            GeoAxesBase.contourf
        )))
        pcolor = _default_transform(_standardize_2d(_cmap_changer(
            GeoAxesBase.pcolor
        )))
        pcolormesh = _default_transform(_standardize_2d(_cmap_changer(
            GeoAxesBase.pcolormesh
        )))
        quiver = _default_transform(_standardize_2d(_cmap_changer(
            GeoAxesBase.quiver
        )))
        streamplot = _default_transform(_standardize_2d(_cmap_changer(
            GeoAxesBase.streamplot
        )))
        barbs = _default_transform(_standardize_2d(_cmap_changer(
            GeoAxesBase.barbs
        )))
        tripcolor = _default_transform(_cmap_changer(
            GeoAxesBase.tripcolor
        ))
        tricontour = _default_transform(_cmap_changer(
            GeoAxesBase.tricontour
        ))
        tricontourf = _default_transform(_cmap_changer(
            GeoAxesBase.tricontourf
        ))


class BasemapAxes(GeoAxes):
    """
    Axes subclass for plotting `~mpl_toolkits.basemap` projections. The
    `~mpl_toolkits.basemap.Basemap` instance is added as the
    `~BasemapAxes.projection` attribute, but you do not have to work with it
    directly -- plotting methods like `matplotlib.axes.Axes.plot` and
    `matplotlib.axes.Axes.contour` are redirected to the corresponding methods on
    the `~mpl_toolkits.basemap.Basemap` instance. Also ``latlon=True`` is passed
    to plotting methods by default.
    """
    #: The registered projection name.
    name = 'basemap'
    _proj_north = ('npaeqd', 'nplaea', 'npstere')
    _proj_south = ('spaeqd', 'splaea', 'spstere')
    _proj_polar = _proj_north + _proj_south
    _proj_non_rectangular = _proj_polar + (  # do not use axes spines as boundaries
        'ortho', 'geos', 'nsper',
        'moll', 'hammer', 'robin',
        'eck4', 'kav7', 'mbtfpq',
        'sinu', 'vandg',
    )

    def __init__(self, *args, map_projection=None, **kwargs):
        """
        Parameters
        ----------
        map_projection : `~mpl_toolkits.basemap.Basemap`
            The `~mpl_toolkits.basemap.Basemap` instance.

        Other parameters
        ----------------
        *args, **kwargs
            Passed to `Axes`.

        See also
        --------
        proplot.axes.GeoAxes
        proplot.constructor.Proj
        """
        # First assign projection and set axis bounds for locators
        # WARNING: Investigated whether Basemap.__init__() could be called
        # twice with updated proj kwargs to modify map bounds after creation
        # and python immmediately crashes. Do not try again.
        import mpl_toolkits.basemap  # noqa: F401 verify available
        if not isinstance(map_projection, mbasemap.Basemap):
            raise ValueError(
                'BasemapAxes requires map_projection=basemap.Basemap'
            )
        self._map_projection = map_projection
        lon0 = self._get_lon0()
        if map_projection.projection in self._proj_polar:
            latmax = 80  # default latmax for gridlines
            extent = [-180 + lon0, 180 + lon0]
            boundinglat = getattr(map_projection, 'boundinglat', 0)
            if map_projection.projection in self._proj_north:
                extent.extend([boundinglat, 90])
            else:
                extent.extend([-90, boundinglat])
        else:
            # NOTE: check out Basemap.__init__
            latmax = 90
            attrs = ('lonmin', 'lonmax', 'latmin', 'latmax')
            extent = [getattr(map_projection, attr, None) for attr in attrs]
            if any(_ is None for _ in extent):
                extent = [180 - lon0, 180 + lon0, -90, 90]  # fallback

        # Initialize axes
        self._map_boundary = None  # start with empty map boundary
        self._has_recurred = False  # use this to override plotting methods
        self._lonlines_major = None  # store gridliner objects this way
        self._lonlines_minor = None
        self._latlines_major = None
        self._latlines_minor = None
        self._lonaxis = _LonAxis(self)
        self._lataxis = _LatAxis(self, latmax=latmax)
        self._set_view_intervals(extent)
        super().__init__(*args, **kwargs)

    def _get_lon0(self):
        """
        Get the central longitude.
        """
        return getattr(self.projection, 'projparams', {}).get('lon_0', 0)

    @staticmethod
    def _iter_gridlines(dict_):
        """
        Iterate over longitude latitude lines.
        """
        dict_ = dict_ or {}
        for pi in dict_.values():
            for pj in pi:
                for obj in pj:
                    yield obj

    def _update_extent(self, lonlim=None, latlim=None, boundinglat=None):
        """
        No-op. Map bounds cannot be changed in basemap.
        """
        if lonlim is not None or latlim is not None or boundinglat is not None:
            warnings._warn_proplot(
                f'Got lonlim={lonlim!r}, latlim={latlim!r}, '
                f'boundinglat={boundinglat!r}, but you cannot "zoom into" a '
                'basemap projection after creating it. Add any of the following '
                "keyword args in your call to plot.Proj('name', basemap=True, ...): "
                "'boundinglat', 'llcrnrlon', 'llcrnrlat', "
                "'urcrnrlon', 'urcrnrlat', 'llcrnrx', 'llcrnry', "
                "'urcrnrx', 'urcrnry', 'width', or 'height'."
            )

    def _update_patches(self, patch_kw=None):
        """
        Update the map boundary patches.
        """
        # Map boundary settings
        kw_face = rc.fill(
            {
                'facecolor': 'axes.facecolor',
                'alpha': 'axes.alpha',
            },
            context=True
        )
        kw_edge = rc.fill(
            {
                'linewidth': 'axes.linewidth',
                'edgecolor': 'axes.edgecolor',
            },
            context=True
        )
        kw_face.update(patch_kw or {})
        self.axesPatch = self.patch  # backwards compatibility

        # Non-rectangularly-bounded projections
        # NOTE: Make sure to turn off clipping by invisible axes boundary. Otherwise
        # get these weird flat edges where map boundaries, latitude/longitude
        # markers come up to the axes bbox
        # * Must set boundary before-hand, otherwise the set_axes_limits method
        #   called by mcontourf/mpcolormesh/etc draws two mapboundary Patch
        #   objects called "limb1" and "limb2" automatically: one for fill and
        #   the other for the edges
        # * Then, since the patch object in _mapboundarydrawn is only the
        #   fill-version, calling drawmapboundary again will replace only *that
        #   one*, but the original visible edges are still drawn -- so e.g. you
        #   can't change the color
        # * If you instead call drawmapboundary right away, _mapboundarydrawn
        #   will contain both the edges and the fill; so calling it again will
        #   replace *both*
        if self.projection.projection in self._proj_non_rectangular:
            self.patch.set_alpha(0)  # make main patch invisible
            if not self.projection._mapboundarydrawn:
                p = self.projection.drawmapboundary(ax=self)
            else:
                p = self.projection._mapboundarydrawn
            p.update(kw_face)
            p.update(kw_edge)
            p.set_rasterized(False)
            p.set_clip_on(False)
            self._map_boundary = p

        # Rectangularly-bounded projections
        else:
            self.patch.update({**kw_face, 'edgecolor': 'none'})
            for spine in self.spines.values():
                spine.update(kw_edge)

    def _update_features(self):
        """
        Update geographic features.
        """
        # NOTE: Also notable are drawcounties, blumarble, drawlsmask,
        # shadedrelief, and etopo methods.
        for name, method in constructor.BASEMAP_FEATURES.items():
            b = rc.get(name, context=True)
            attr = f'_{name}_feature'
            if b is not None:
                feat = getattr(self, attr, None)
                if not b:
                    if feat is not None:  # toggle existing feature off
                        for obj in feat:
                            feat.set_visible(False)
                else:
                    drawn = feat is not None  # if exists, apply *updated* settings
                    if not drawn:
                        feat = getattr(self.projection, method)(ax=self)
                    kw = rc.category(name, context=drawn)
                    if not isinstance(feat, (list, tuple)):  # list of artists?
                        feat = (feat,)
                    for obj in feat:
                        obj.update(kw)
                    setattr(self, attr, feat)

    def _update_gridlines(
        self, which='major', longrid=None, latgrid=None, lonarray=None, latarray=None,
    ):
        """
        Apply changes to the basemap axes. Extra kwargs are used
        to update the proj4 params.
        """
        latmax = self._lataxis.get_latmax()
        for name, grid, array, method in zip(
            ('lon', 'lat'),
            (longrid, latgrid),
            (lonarray, latarray),
            ('drawmeridians', 'drawparallels'),
        ):
            # Correct lonarray and latarray, change fromm lrbt to lrtb
            if array is not None:
                array = list(array)
                array[2:] = array[2:][::-1]
            axis = getattr(self, f'_{name}axis')

            # Toggle gridlines
            attr = f'_{name}lines_{which}'
            objs = getattr(self, attr)  # dictionary of previous objects
            lines = getattr(self, f'_get_{name}ticklocs')(which=which)
            if which == 'major':
                attrs = ('isDefault_majloc', 'isDefault_majfmt')
            else:
                attrs = ('isDefault_minloc',)
            rebuild = (
                not objs
                or any(_ is not None for _ in array)
                or any(not getattr(axis, _) for _ in attrs)
            )

            # Draw or redraw meridian or parallel lines
            # Also mark formatters and locators as 'default'
            if rebuild:
                kwdraw = {}
                for _ in attrs:
                    setattr(axis, _, True)
                formatter = axis.get_major_formatter()
                if formatter is not None:  # use functional formatter
                    kwdraw['fmt'] = formatter
                for obj in self._iter_gridlines(objs):
                    obj.set_visible(False)
                objs = getattr(self.projection, method)(
                    lines, ax=self, latmax=latmax, labels=array, **kwdraw
                )
                setattr(self, attr, objs)

            # Update gridline settings
            rc_mode = rc._get_context_mode()
            kwlines, kwtext = self._get_gridline_props(
                which=which, context=(not rebuild and rc_mode == 2)
            )
            for obj in self._iter_gridlines(objs):
                if isinstance(obj, mtext.Text):
                    obj.update(kwtext)
                else:
                    obj.update(kwlines)

            # Toggle existing gridlines on and off
            if grid is not None:
                for obj in self._iter_gridlines(objs):
                    obj.set_visible(grid)

    def _update_major_gridlines(
        self,
        longrid=None, latgrid=None, lonarray=None, latarray=None,
        loninline=None, latinline=None, rotate_labels=None, labelpad=None,
    ):
        """
        Update major gridlines.
        """
        loninline, latinline, labelpad, rotate_labels  # avoid U100 error
        self._update_gridlines(
            which='major',
            longrid=longrid, latgrid=latgrid, lonarray=lonarray, latarray=latarray,
        )

    def _update_minor_gridlines(
        self, longrid=None, latgrid=None, lonarray=None, latarray=None,
    ):
        """
        Update minor gridlines.
        """
        lonarray, latarray  # prevent U100 error (ignore these)
        array = [None] * 4  # NOTE: must be None not False (see _update_gridlines)
        self._update_gridlines(
            which='minor',
            longrid=longrid, latgrid=latgrid, lonarray=array, latarray=array,
        )

    @property
    def projection(self):
        """
        The `~mpl_toolkits.basemap.Basemap` instance associated with this axes.
        """
        return self._map_projection

    @projection.setter
    def projection(self, map_projection):
        if not isinstance(map_projection, mbasemap.Basemap):
            raise ValueError('Projection must be a basemap.Basemap instance.')
        self._map_projection = map_projection

    # Wrapped methods
    plot = _basemap_norecurse(_default_latlon(_plot_wrapper(_standardize_1d(
        _indicate_error(_cycle_changer(_basemap_redirect(maxes.Axes.plot)))
    ))))
    scatter = _basemap_norecurse(_default_latlon(_scatter_wrapper(_standardize_1d(
        _indicate_error(_cycle_changer(_basemap_redirect(maxes.Axes.scatter)))
    ))))
    contour = _basemap_norecurse(_default_latlon(_standardize_2d(_cmap_changer(
        _basemap_redirect(maxes.Axes.contour)
    ))))
    contourf = _basemap_norecurse(_default_latlon(_standardize_2d(_cmap_changer(
        _basemap_redirect(maxes.Axes.contourf)
    ))))
    pcolor = _basemap_norecurse(_default_latlon(_standardize_2d(_cmap_changer(
        _basemap_redirect(maxes.Axes.pcolor)
    ))))
    pcolormesh = _basemap_norecurse(_default_latlon(_standardize_2d(_cmap_changer(
        _basemap_redirect(maxes.Axes.pcolormesh)
    ))))
    quiver = _basemap_norecurse(_default_latlon(_standardize_2d(_cmap_changer(
        _basemap_redirect(maxes.Axes.quiver)
    ))))
    streamplot = _basemap_norecurse(_default_latlon(_standardize_2d(_cmap_changer(
        _basemap_redirect(maxes.Axes.streamplot)
    ))))
    barbs = _basemap_norecurse(_default_latlon(_standardize_2d(_cmap_changer(
        _basemap_redirect(maxes.Axes.barbs)
    ))))
    hexbin = _basemap_norecurse(_standardize_1d(_cmap_changer(
        _basemap_redirect(maxes.Axes.hexbin)
    )))
    imshow = _basemap_norecurse(_cmap_changer(
        _basemap_redirect(maxes.Axes.imshow)
    ))
